package com.operator.companion.pb

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.OpenableColumns
import android.util.Log
import android.webkit.MimeTypeMap
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import com.operator.companion.MainActivity
import com.operator.companion.network.CompanionWebSocketClient
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.RandomAccessFile
import java.security.MessageDigest
import java.util.UUID

/**
 * PowerBoosters client state. Everything the phone sends goes through a persisted outbox and is
 * idempotent on the laptop (client ids), so going offline mid-way never loses or duplicates work.
 */
object PbStore {
    private const val TAG = "PowerBoosters"
    private const val CHUNK = 128 * 1024
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var app: Context? = null

    data class Upload(
        val uploadId: String, val path: String, val name: String, val mime: String, val size: Long,
        val sha: String, val purpose: String, val assignmentId: String = "", val fileId: String = "",
        val received: Long = 0, val status: String = "queued", val error: String = ""
    ) {
        fun toJson(): JSONObject = JSONObject().put("uploadId", uploadId).put("path", path).put("name", name).put("mime", mime)
            .put("size", size).put("sha", sha).put("purpose", purpose).put("assignmentId", assignmentId).put("fileId", fileId)
            .put("received", received).put("status", status).put("error", error)
        val kind: String get() = when {
            mime.startsWith("image/") -> "image"; mime.startsWith("video/") -> "video"
            mime.startsWith("audio/") -> "audio"; else -> "document"
        }
        companion object {
            fun from(o: JSONObject) = Upload(o.getString("uploadId"), o.getString("path"), o.getString("name"), o.getString("mime"),
                o.getLong("size"), o.getString("sha"), o.getString("purpose"), o.optString("assignmentId"), o.optString("fileId"),
                o.optLong("received"), o.optString("status", "queued"), o.optString("error"))
        }
    }

    val online = MutableStateFlow(false)
    val profile = MutableStateFlow<JSONObject?>(null)
    val leaderboard = MutableStateFlow<JSONObject?>(null)
    val assignments = MutableStateFlow<List<JSONObject>>(emptyList())
    val messages = MutableStateFlow<List<JSONObject>>(emptyList())
    val outbox = MutableStateFlow<List<JSONObject>>(emptyList())
    val uploads = MutableStateFlow<Map<String, Upload>>(emptyMap())
    val notifications = MutableStateFlow<List<JSONObject>>(emptyList())
    val adminReadSeq = MutableStateFlow(0L)
    val memberReadSeq = MutableStateFlow(0L)   // what this phone has read (drives the chat badge)
    val commanderTyping = MutableStateFlow(false)
    val reward = MutableStateFlow<JSONObject?>(null)
    val submitted = MutableStateFlow<JSONObject?>(null)     // last accepted submission (confirmation screen)
    val error = MutableStateFlow<String?>(null)
    val downloads = MutableStateFlow<Map<String, String>>(emptyMap())    // file id -> local path when complete
    val downloadProgress = MutableStateFlow<Map<String, Float>>(emptyMap())
    @Volatile var chatVisible = false
    val openRequested = MutableStateFlow(false)
    /** Player name shown on the leaderboard instead of the device model. Null until chosen. */
    val username = MutableStateFlow<String?>(null)
    val usernameError = MutableStateFlow<String?>(null)   // a PowerBoosters notification was tapped

    private fun ws() = CompanionWebSocketClient.activeInstance
    private fun prefs() = app?.getSharedPreferences("powerboosters", Context.MODE_PRIVATE)

    fun init(context: Context) {
        if (app != null) return
        app = context.applicationContext
        runCatching {
            val p = prefs()!!
            username.value = p.getString("username", null)
            uploads.value = JSONArray(p.getString("uploads", "[]")).let { a -> (0 until a.length()).map { Upload.from(a.getJSONObject(it)) } }
                .associateBy { it.uploadId }
            outbox.value = JSONArray(p.getString("outbox", "[]")).let { a -> (0 until a.length()).map { a.getJSONObject(it) } }
            val cached = File(app!!.filesDir, "pb_state.json")
            if (cached.exists()) applyState(JSONObject(cached.readText()), persist = false)
        }.onFailure { Log.w(TAG, "restore failed: ${it.message}") }
    }

    private fun persist() {
        val p = prefs() ?: return
        p.edit().putString("uploads", JSONArray(uploads.value.values.map { it.toJson() }).toString())
            .putString("outbox", JSONArray(outbox.value).toString()).apply()
    }

    private fun send(type: String, payload: JSONObject = JSONObject()): Boolean =
        ws()?.sendDirectJson(JSONObject().put("type", type).put("payload", payload)) ?: false

    fun log(level: String, op: String, message: String) {
        Log.w(TAG, "$op: $message")
        runCatching { send("PB_LOG", JSONObject().put("level", level).put("op", op).put("message", message)) }
    }

    private inline fun safe(op: String, block: () -> Unit) {
        try { block() } catch (e: Exception) { log("error", op, e.toString()) }
    }

    // ---------------- connection lifecycle (offline sync) ----------------
    fun onConnected(context: Context) = safe("connect") {
        init(context)
        online.value = true
        val lastMsg = 0L // full snapshot: small squads, and it repairs any missed live event
        send("PB_SYNC", JSONObject().put("msg_after", lastMsg).put("notif_after", 0))
        uploads.value.values.filter { it.status != "ready" && it.status != "error" }.forEach { initUpload(it) }
        flushOutbox()
        if (prefs()?.getBoolean("username_pending", false) == true) username.value?.let { sendUsername(it) }
    }

    /** Saved on the phone first (works offline); the laptop confirms it is unique when the link is up. */
    fun chooseUsername(context: Context, name: String) {
        init(context)
        val n = name.trim()
        if (!Regex("[A-Za-z][A-Za-z0-9_.]{2,19}").matches(n)) {
            usernameError.value = "3–20 letters, numbers, _ or . — start with a letter"; return
        }
        usernameError.value = null
        username.value = n
        prefs()?.edit()?.putString("username", n)?.putBoolean("username_pending", true)?.apply()
        sendUsername(n)
    }

    private fun sendUsername(n: String) { send("PB_SET_USERNAME", JSONObject().put("username", n)) }

    fun onDisconnected() {
        online.value = false
        commanderTyping.value = false
    }

    fun sync() { send("PB_SYNC", JSONObject()) }

    /** The link can come up before this screen exists: read it from the socket instead of only from events. */
    fun refreshOnline() {
        val up = ws()?.isLinkUp() == true
        if (up && !online.value) app?.let { onConnected(it) } else online.value = up
    }

    // ---------------- incoming ----------------
    fun onMessage(type: String, p: JSONObject) = safe(type) {
        when (type) {
            "PB_STATE" -> applyState(p, persist = true)
            "PB_MSG" -> { mergeMessage(p); dropOutbox(p.optString("client_id")) ; if (chatVisible) markRead() }
            "PB_ASSIGNMENT" -> mergeAssignment(p)
            "PB_PROFILE" -> { profile.value = p.optJSONObject("profile"); leaderboard.value = p.optJSONObject("leaderboard"); adoptUsername() }
            "PB_USERNAME_OK" -> {
                username.value = p.optString("username")
                prefs()?.edit()?.putString("username", username.value)?.putBoolean("username_pending", false)?.apply()
            }
            "PB_RECEIPT" -> adminReadSeq.value = p.optLong("admin_read_seq", adminReadSeq.value)
            "PB_TYPING" -> {
                commanderTyping.value = p.optBoolean("typing")
                if (commanderTyping.value) scope.launch { delay(6000); commanderTyping.value = false }
            }
            "PB_NOTIFY" -> {
                notifications.value = listOf(p) + notifications.value.filter { it.optLong("seq") != p.optLong("seq") }
                val isChat = p.optString("kind") == "message"
                if (!(isChat && chatVisible)) postOsNotification(p)
                if (p.optString("kind") in setOf("mission", "rejected", "revision", "approved")) sync()
            }
            "PB_REWARD" -> reward.value = p
            "PB_SUBMIT_OK" -> {
                dropOutbox(p.optString("client_id"))
                mergeAssignment(p.getJSONObject("assignment"))
                submitted.value = p.getJSONObject("submission")
            }
            "PB_UPLOAD_STATUS" -> onUploadStatus(p)
            "PB_ERROR" -> if (p.optString("op") == "PB_SET_USERNAME") {
                // taken or invalid: ask again
                prefs()?.edit()?.remove("username")?.putBoolean("username_pending", false)?.apply()
                username.value = null
                usernameError.value = p.optString("message")
            } else onError(p)
        }
    }

    /** Reinstalled app: the laptop still knows this phone's username. */
    private fun adoptUsername() {
        val server = profile.value?.optString("username")?.takeIf { it.isNotBlank() && it != "null" } ?: return
        if (prefs()?.getBoolean("username_pending", false) == true) return
        if (username.value != server) {
            username.value = server
            prefs()?.edit()?.putString("username", server)?.apply()
        }
    }

    private fun applyState(p: JSONObject, persist: Boolean) {
        profile.value = p.optJSONObject("profile")
        adoptUsername()
        leaderboard.value = p.optJSONObject("leaderboard")
        assignments.value = p.optJSONArray("assignments").objs()
        val msgs = p.optJSONArray("messages").objs()
        messages.value = (messages.value.filter { m -> msgs.none { it.optString("id") == m.optString("id") } } + msgs)
            .sortedBy { it.optLong("seq") }
        msgs.forEach { dropOutbox(it.optString("client_id")) }
        p.optJSONObject("conversation")?.let {
            adminReadSeq.value = it.optLong("admin_read_seq")
            memberReadSeq.value = maxOf(memberReadSeq.value, it.optLong("member_read_seq"))
        }
        notifications.value = p.optJSONArray("notifications").objs()
        if (persist) app?.let { File(it.filesDir, "pb_state.json").writeText(p.toString()) }
    }

    private fun mergeMessage(m: JSONObject) {
        messages.value = (messages.value.filter { it.optString("id") != m.optString("id") } + m).sortedBy { it.optLong("seq") }
    }

    private fun mergeAssignment(a: JSONObject) {
        val id = a.optString("id")
        val list = assignments.value
        assignments.value = if (list.any { it.optString("id") == id }) list.map { if (it.optString("id") == id) a else it } else listOf(a) + list
    }

    private fun onError(p: JSONObject) {
        val ref = p.optString("ref")
        val msg = p.optString("message", "Something went wrong")
        uploads.value[ref]?.let { setUpload(it.copy(status = "error", error = msg)) }
        val item = outbox.value.firstOrNull { it.optString("client_id") == ref }
        if (item != null && p.optInt("status") in 400..499) {
            // the laptop refused it (closed mission, no attempts, bad format): show why, do not retry forever
            dropOutbox(ref)
            if (item.optString("kind") == "submit") item.put("failed", msg)
        }
        error.value = msg
    }

    // ---------------- chat ----------------
    fun sendMessage(text: String, replyTo: String = "", uploadIds: List<String> = emptyList()) = safe("send") {
        if (text.isBlank() && uploadIds.isEmpty()) return
        val item = JSONObject().put("kind", "msg").put("client_id", "c" + UUID.randomUUID().toString().replace("-", ""))
            .put("text", text).put("reply_to", replyTo).put("uploads", JSONArray(uploadIds)).put("created_at", System.currentTimeMillis() / 1000.0)
        outbox.value = outbox.value + item
        persist()
        flushOutbox()
    }

    fun react(messageId: String, emoji: String) { send("PB_REACT", JSONObject().put("message_id", messageId).put("emoji", emoji)) }

    private var lastTyping = 0L
    fun typing() {
        val now = System.currentTimeMillis()
        if (now - lastTyping > 3000) { lastTyping = now; send("PB_TYPING", JSONObject().put("typing", true)) }
    }

    fun markRead() {
        val top = messages.value.lastOrNull()?.optLong("seq") ?: return
        memberReadSeq.value = maxOf(memberReadSeq.value, top)
        send("PB_READ", JSONObject().put("seq", top))
    }

    fun markNotificationsRead() {
        notifications.value = notifications.value.map { JSONObject(it.toString()).put("read", 1) }
        send("PB_NOTIF_READ")
    }

    // ---------------- missions ----------------
    fun accept(assignmentId: String) { if (!send("PB_ACCEPT", JSONObject().put("assignment_id", assignmentId))) error.value = "Offline — try again when connected" }

    fun submit(assignmentId: String, text: String, comment: String, uploadIds: List<String>): String {
        val cid = "s" + UUID.randomUUID().toString().replace("-", "")
        val item = JSONObject().put("kind", "submit").put("client_id", cid).put("assignment_id", assignmentId)
            .put("text", text).put("comment", comment).put("uploads", JSONArray(uploadIds))
        outbox.value = outbox.value + item
        persist()
        flushOutbox()
        return cid
    }

    /** Sends every outbox item whose attachments have finished uploading. Safe to call any time. */
    fun flushOutbox() = safe("flush") {
        if (ws()?.isLinkUp() != true) return
        for (item in outbox.value) {
            val ids = item.optJSONArray("uploads").strings()
            val ups = ids.mapNotNull { uploads.value[it] }
            if (ups.size != ids.size) { dropOutbox(item.optString("client_id")); continue } // attachment removed
            if (ups.any { it.status != "ready" }) continue
            val files = JSONArray(ups.map { it.fileId })
            when (item.optString("kind")) {
                "msg" -> send("PB_MSG_SEND", JSONObject().put("client_id", item.getString("client_id")).put("text", item.optString("text"))
                    .put("reply_to", item.optString("reply_to")).put("attachments", files))
                "submit" -> send("PB_SUBMIT", JSONObject().put("client_id", item.getString("client_id"))
                    .put("assignment_id", item.getString("assignment_id")).put("text", item.optString("text"))
                    .put("comment", item.optString("comment")).put("files", files))
            }
        }
    }

    private fun dropOutbox(clientId: String) {
        if (clientId.isEmpty() || outbox.value.none { it.optString("client_id") == clientId }) return
        outbox.value = outbox.value.filter { it.optString("client_id") != clientId }
        persist()
    }

    // ---------------- uploads (resumable) ----------------
    /** Copies the picked content into app storage first so an interrupted upload can resume after a restart. */
    fun addUpload(context: Context, uri: Uri, purpose: String, assignmentId: String = "", forcedMime: String? = null,
                  onReady: (Upload) -> Unit) {
        init(context)
        scope.launch {
            try {
                val cr = context.contentResolver
                var name = "file"
                runCatching {
                    cr.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { c -> if (c.moveToFirst()) name = c.getString(0) ?: name }
                }
                if (uri.scheme == "file") name = File(uri.path ?: "file").name
                val mime = forcedMime ?: cr.getType(uri) ?: MimeTypeMap.getSingleton()
                    .getMimeTypeFromExtension(name.substringAfterLast('.', "").lowercase()) ?: "application/octet-stream"
                if (!name.contains('.')) MimeTypeMap.getSingleton().getExtensionFromMimeType(mime)?.let { name = "$name.$it" }
                val id = "u" + UUID.randomUUID().toString().replace("-", "").take(24)
                val dir = File(context.filesDir, "pb_outbox").apply { mkdirs() }
                val dest = File(dir, "${id}_${name.replace(Regex("[^A-Za-z0-9._-]"), "_")}")
                val md = MessageDigest.getInstance("SHA-256")
                (if (uri.scheme == "file") File(uri.path!!).inputStream() else cr.openInputStream(uri))!!.use { input ->
                    dest.outputStream().use { out ->
                        val buf = ByteArray(64 * 1024)
                        while (true) { val n = input.read(buf); if (n < 0) break; md.update(buf, 0, n); out.write(buf, 0, n) }
                    }
                }
                val up = Upload(id, dest.absolutePath, name, mime, dest.length(), md.digest().joinToString("") { "%02x".format(it) },
                    purpose, assignmentId)
                setUpload(up)
                kotlinx.coroutines.withContext(Dispatchers.Main) { onReady(up) }
                initUpload(up)
            } catch (e: Exception) {
                error.value = "Could not read that file: ${e.message}"
                log("error", "attach", e.toString())
            }
        }
    }

    fun retryUpload(id: String) { uploads.value[id]?.let { setUpload(it.copy(status = "queued", error = "")); initUpload(uploads.value[id]!!) } }

    fun removeUpload(id: String) {
        uploads.value[id]?.let { runCatching { File(it.path).delete() } }
        uploads.value = uploads.value - id
        persist()
    }

    private fun setUpload(u: Upload) { uploads.value = uploads.value + (u.uploadId to u); persist() }

    private fun initUpload(u: Upload) {
        if (u.status == "ready") return
        send("PB_UPLOAD_INIT", JSONObject().put("upload_id", u.uploadId).put("name", u.name).put("mime", u.mime)
            .put("size", u.size).put("sha256", u.sha).put("purpose", u.purpose).put("assignment_id", u.assignmentId))
    }

    private fun onUploadStatus(p: JSONObject) {
        val u = uploads.value[p.optString("upload_id")] ?: uploads.value.values.firstOrNull { it.fileId == p.optString("file_id") } ?: return
        if (p.has("error")) {
            setUpload(u.copy(status = "error", error = p.optString("error")))
            if (p.optString("error").contains("Checksum")) scope.launch { delay(1500); retryUpload(u.uploadId) }
            return
        }
        val got = p.optLong("received")
        val upd = u.copy(fileId = p.optString("file_id"), received = got, status = if (p.optString("status") == "ready") "ready" else "uploading")
        setUpload(upd)
        if (upd.status == "ready") { flushOutbox(); return }
        scope.launch { sendChunk(upd) }
    }

    private suspend fun sendChunk(u: Upload) {
        val f = File(u.path)
        if (!f.exists()) { setUpload(u.copy(status = "error", error = "File missing on phone")); return }
        val len = minOf(CHUNK.toLong(), u.size - u.received).toInt()
        val data = ByteArray(len)
        RandomAccessFile(f, "r").use { it.seek(u.received); it.readFully(data) }
        val header = JSONObject().put("type", "PB_CHUNK").put("file_id", u.fileId).put("offset", u.received).put("upload_id", u.uploadId)
        var tries = 0
        while (ws()?.sendPbBinary(header, data) != true) {
            if (ws()?.isLinkUp() != true || ++tries > 300) return   // resumes from the laptop's offset on reconnect
            delay(100)
        }
    }

    // ---------------- downloads (authorised per file by the laptop) ----------------
    private val pulls = HashMap<String, File>()

    fun localFile(fileId: String): File? = downloads.value[fileId]?.let { File(it) }?.takeIf { it.exists() }

    fun download(context: Context, fileId: String, name: String) {
        init(context)
        if (localFile(fileId) != null || pulls.containsKey(fileId)) return
        val dir = File(context.cacheDir, "pb_files").apply { mkdirs() }
        val f = File(dir, "${fileId}_${name.replace(Regex("[^A-Za-z0-9._-]"), "_")}")
        if (f.exists() && f.length() > 0 && !File(f.path + ".part").exists()) { downloads.value = downloads.value + (fileId to f.path); return }
        synchronized(pulls) { pulls[fileId] = f }
        File(f.path + ".part").writeText("")
        f.delete()
        if (!send("PB_FILE_PULL", JSONObject().put("file_id", fileId).put("offset", 0))) synchronized(pulls) { pulls.remove(fileId) }
    }

    fun onFileChunk(h: JSONObject, data: ByteArray) = safe("download") {
        val id = h.optString("file_id")
        val f = synchronized(pulls) { pulls[id] } ?: return
        val off = h.optLong("offset")
        if (off != (if (f.exists()) f.length() else 0L)) return
        f.appendBytes(data)
        val size = h.optLong("size").coerceAtLeast(1)
        downloadProgress.value = downloadProgress.value + (id to (f.length().toFloat() / size))
        if (h.optBoolean("eof")) {
            synchronized(pulls) { pulls.remove(id) }
            File(f.path + ".part").delete()
            downloads.value = downloads.value + (id to f.path)
        } else send("PB_FILE_PULL", JSONObject().put("file_id", id).put("offset", f.length()))
    }

    // ---------------- OS notifications ----------------
    private fun postOsNotification(n: JSONObject) {
        val ctx = app ?: return
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(ctx, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return
        val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= 26 && nm.getNotificationChannel("pb_alerts") == null) {
            nm.createNotificationChannel(NotificationChannel("pb_alerts", "PowerBoosters", NotificationManager.IMPORTANCE_HIGH))
        }
        val open = PendingIntent.getActivity(ctx, 7, Intent(ctx, MainActivity::class.java).putExtra("powerboosters", true)
            .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val notif = NotificationCompat.Builder(ctx, "pb_alerts")
            .setSmallIcon(android.R.drawable.star_on)
            .setContentTitle(n.optString("title"))
            .setContentText(n.optString("body"))
            .setStyle(NotificationCompat.BigTextStyle().bigText(n.optString("body")))
            .setAutoCancel(true).setContentIntent(open)
            .setPriority(NotificationCompat.PRIORITY_HIGH).build()
        runCatching { nm.notify(5000 + (n.optLong("seq") % 1000).toInt(), notif) }
    }
}

fun JSONArray?.objs(): List<JSONObject> = if (this == null) emptyList() else (0 until length()).mapNotNull { optJSONObject(it) }
fun JSONArray?.strings(): List<String> = if (this == null) emptyList() else (0 until length()).map { optString(it) }
