package com.operator.companion.pb

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.BitmapFactory
import android.media.MediaRecorder
import android.net.Uri
import android.os.Build
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.*
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.scaleIn
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.lerp
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

// ---------- PowerBoosters palette (purple/blue gaming) ----------
private val Bg = Color(0xFF080A1A)
private val Card = Color(0xFF141733)
private val Card2 = Color(0xFF1C1F45)
private val Purple = Color(0xFF8B5CF6)
private val Blue = Color(0xFF3B82F6)
private val Cyan = Color(0xFF22D3EE)
private val Pink = Color(0xFFEC4899)
private val Green = Color(0xFF22C55E)
private val Amber = Color(0xFFF59E0B)
private val Red = Color(0xFFEF4444)
private val Muted = Color(0xFF94A3B8)
private val Grad = Brush.linearGradient(listOf(Purple, Blue))

private fun prioColor(p: String) = when (p) { "critical" -> Red; "high" -> Amber; "low" -> Muted; else -> Cyan }
private fun statusLabel(s: String) = when (s) {
    "assigned" -> "NEW" to Cyan; "in_progress" -> "IN PROGRESS" to Blue; "submitted" -> "UNDER REVIEW" to Amber
    "revision_required" -> "ACTION REQUIRED" to Pink; "completed" -> "VERIFIED" to Green; "failed" -> "CLOSED" to Red
    else -> s.uppercase() to Muted
}
private fun fmtTime(sec: Double): String =
    if (sec <= 0) "" else SimpleDateFormat("d MMM, HH:mm", Locale.getDefault()).format(Date((sec * 1000).toLong()))

private fun openFile(ctx: Context, f: File, mime: String) {
    runCatching {
        val uri = FileProvider.getUriForFile(ctx, ctx.packageName + ".pbfiles", f)
        ctx.startActivity(Intent(Intent.ACTION_VIEW).setDataAndType(uri, mime).addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK))
    }.onFailure { PbStore.error.value = "No app on this phone can open that file" }
}

private fun captureUri(ctx: Context, ext: String): Pair<Uri, File> {
    val dir = File(ctx.cacheDir, "pb_capture").apply { mkdirs() }
    val f = File(dir, "capture_${System.currentTimeMillis()}.$ext")
    return FileProvider.getUriForFile(ctx, ctx.packageName + ".pbfiles", f) to f
}

private fun granted(ctx: Context, perm: String) = ContextCompat.checkSelfPermission(ctx, perm) == PackageManager.PERMISSION_GRANTED

// =====================================================================
@Composable
fun PowerBoostersApp(onExit: () -> Unit) {
    val ctx = LocalContext.current
    LaunchedEffect(Unit) { PbStore.init(ctx); PbStore.sync(); while (true) { PbStore.refreshOnline(); kotlinx.coroutines.delay(2000) } }
    var tab by remember { mutableIntStateOf(0) }
    var openAssignment by remember { mutableStateOf<String?>(null) }
    val notifs by PbStore.notifications.collectAsState()
    val online by PbStore.online.collectAsState()
    val profile by PbStore.profile.collectAsState()
    val err by PbStore.error.collectAsState()
    val unreadAlerts = notifs.count { it.optInt("read") == 0 }

    // OS notifications on Android 13+ need the person's consent: ask once, visibly.
    val notifPerm = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { }
    LaunchedEffect(Unit) {
        if (Build.VERSION.SDK_INT >= 33 && !granted(ctx, Manifest.permission.POST_NOTIFICATIONS)) notifPerm.launch(Manifest.permission.POST_NOTIFICATIONS)
    }
    BackHandler { if (openAssignment != null) openAssignment = null else onExit() }

    Box(Modifier.fillMaxSize().background(Bg)) {
        Column(Modifier.fillMaxSize()) {
            // header
            Row(Modifier.fillMaxWidth().background(Brush.verticalGradient(listOf(Color(0xFF1A1442), Bg))).padding(16.dp, 14.dp),
                verticalAlignment = Alignment.CenterVertically) {
                Text("←", color = Color.White, fontSize = 22.sp, modifier = Modifier.clickable { if (openAssignment != null) openAssignment = null else onExit() }.padding(end = 12.dp))
                Column(Modifier.weight(1f)) {
                    Text("POWERBOOSTERS", color = Color.White, fontWeight = FontWeight.Black, fontSize = 18.sp, letterSpacing = 1.5.sp, maxLines = 1, softWrap = false)
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Box(Modifier.size(8.dp).clip(CircleShape).background(if (online) Green else Muted))
                        Text(if (online) "  Linked to HQ" else "  Offline — syncs later", color = Muted, fontSize = 11.sp, maxLines = 1)
                    }
                }
                profile?.let { p ->
                    Box(Modifier.clip(RoundedCornerShape(20.dp)).background(Grad).padding(horizontal = 12.dp, vertical = 6.dp)) {
                        Text("LV ${p.optInt("level", 1)} · ${p.optInt("xp")} XP", color = Color.White, fontWeight = FontWeight.Bold, fontSize = 12.sp)
                    }
                }
            }
            Box(Modifier.weight(1f)) {
                val aid = openAssignment
                if (aid != null) MissionDetail(aid) { openAssignment = null }
                else when (tab) {
                    0 -> MissionsTab { openAssignment = it }
                    1 -> ChatTab()
                    2 -> SquadTab()
                    else -> AlertsTab()
                }
            }
            // bottom navigation
            Row(Modifier.fillMaxWidth().background(Color(0xFF0E1028)).padding(vertical = 6.dp), horizontalArrangement = Arrangement.SpaceEvenly) {
                val readUpTo by PbStore.memberReadSeq.collectAsState()
                val unreadChat = PbStore.messages.collectAsState().value.count { it.optString("sender") == "commander" && it.optLong("seq") > readUpTo }
                listOf(Triple("🎯", "Missions", 0), Triple("💬", "Chat", unreadChat), Triple("🏆", "Squad", 0), Triple("🔔", "Alerts", unreadAlerts))
                    .forEachIndexed { i, (icon, label, badge) ->
                        Column(Modifier.weight(1f).clickable { tab = i; openAssignment = null; if (i == 3) PbStore.markNotificationsRead() }.padding(6.dp),
                            horizontalAlignment = Alignment.CenterHorizontally) {
                            Box {
                                Text(icon, fontSize = 20.sp)
                                if (badge > 0 && tab != i) Box(Modifier.align(Alignment.TopEnd).offset(8.dp, (-4).dp).clip(CircleShape).background(Pink).padding(horizontal = 5.dp)) {
                                    Text("$badge", color = Color.White, fontSize = 9.sp, fontWeight = FontWeight.Bold)
                                }
                            }
                            Text(label, color = if (tab == i) Color.White else Muted, fontSize = 11.sp, fontWeight = if (tab == i) FontWeight.Bold else FontWeight.Normal)
                            Box(Modifier.padding(top = 3.dp).width(22.dp).height(3.dp).clip(RoundedCornerShape(2.dp)).background(if (tab == i) Grad else Brush.linearGradient(listOf(Color.Transparent, Color.Transparent))))
                        }
                    }
            }
        }
        RewardOverlay()
        SubmittedOverlay()
        if (err != null) {
            Box(Modifier.align(Alignment.TopCenter).padding(top = 80.dp, start = 16.dp, end = 16.dp).clip(RoundedCornerShape(14.dp))
                .background(Color(0xFF3B1530)).border(1.dp, Pink, RoundedCornerShape(14.dp)).clickable { PbStore.error.value = null }.padding(14.dp)) {
                Text("⚠ $err", color = Color.White, fontSize = 13.sp)
            }
            LaunchedEffect(err) { kotlinx.coroutines.delay(5000); PbStore.error.value = null }
        }
    }
}

// ---------------- Missions ----------------
@Composable
private fun MissionsTab(onOpen: (String) -> Unit) {
    val list by PbStore.assignments.collectAsState()
    if (list.isEmpty()) {
        Empty("🛰️", "No missions yet", "When the Commander deploys a mission it lands here — even if you're offline.")
        return
    }
    val order = listOf("revision_required", "assigned", "in_progress", "submitted", "completed", "failed")
    LazyColumn(contentPadding = PaddingValues(14.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        items(list.sortedBy { order.indexOf(it.optString("status")) }, key = { it.optString("id") }) { a ->
            val m = a.getJSONObject("mission")
            val (label, color) = statusLabel(a.optString("status"))
            Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(18.dp)).background(Card)
                .border(1.dp, Brush.linearGradient(listOf(prioColor(m.optString("priority")).copy(alpha = 0.7f), Purple.copy(alpha = 0.2f))), RoundedCornerShape(18.dp))
                .clickable { onOpen(a.optString("id")) }.padding(16.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Chip(m.optString("priority").uppercase(), prioColor(m.optString("priority")))
                    Spacer(Modifier.width(6.dp))
                    Chip(if (m.optString("distribution") == "targeted") "EXCLUSIVE" else "SQUAD", Purple)
                    Spacer(Modifier.weight(1f))
                    Text("+${m.optInt("xp_reward") + m.optInt("bonus_xp")} XP", color = Amber, fontWeight = FontWeight.Black, fontSize = 15.sp)
                }
                Spacer(Modifier.height(10.dp))
                Text(m.optString("title"), color = Color.White, fontWeight = FontWeight.Bold, fontSize = 17.sp)
                if (m.optString("description").isNotBlank())
                    Text(m.optString("description"), color = Muted, fontSize = 13.sp, maxLines = 2, overflow = TextOverflow.Ellipsis)
                Spacer(Modifier.height(10.dp))
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Chip(label, color)
                    Spacer(Modifier.weight(1f))
                    val dl = m.optDouble("deadline", 0.0)
                    if (dl > 0) Text("⏱ ${fmtTime(dl)}", color = Muted, fontSize = 11.sp)
                }
            }
        }
    }
}

@Composable
private fun MissionDetail(assignmentId: String, onBack: () -> Unit) {
    val ctx = LocalContext.current
    val list by PbStore.assignments.collectAsState()
    val a = list.firstOrNull { it.optString("id") == assignmentId } ?: run { onBack(); return }
    val m = a.getJSONObject("mission")
    val status = a.optString("status")
    var submitting by remember(assignmentId) { mutableStateOf(false) }
    if (submitting) { SubmitForm(a) { submitting = false }; return }
    val subs = a.optJSONArray("submissions").objs()
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp)) {
        val (label, color) = statusLabel(status)
        Row { Chip(label, color); Spacer(Modifier.width(6.dp)); Chip(m.optString("priority").uppercase(), prioColor(m.optString("priority"))) }
        Spacer(Modifier.height(10.dp))
        Text(m.optString("title"), color = Color.White, fontSize = 24.sp, fontWeight = FontWeight.Black)
        Text("Reward +${m.optInt("xp_reward")} XP" + (if (m.optInt("bonus_xp") > 0) " · bonus +${m.optInt("bonus_xp")}" else "") +
            " · attempts left ${a.optInt("attempts_left")} of ${m.optInt("max_attempts")}", color = Amber, fontSize = 13.sp)
        val dl = m.optDouble("deadline", 0.0)
        if (dl > 0) Text("Deadline ${fmtTime(dl)}", color = Muted, fontSize = 12.sp)

        // the latest feedback first: the member must see exactly what to fix
        val lastReview = subs.lastOrNull()?.optJSONArray("reviews").objs().lastOrNull()
        if (status == "revision_required" && lastReview != null) {
            Spacer(Modifier.height(14.dp))
            Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(16.dp)).background(Color(0xFF3B1530)).border(1.dp, Pink, RoundedCornerShape(16.dp)).padding(14.dp)) {
                Text(if (lastReview.optString("action") == "reject") "MISSION NOT APPROVED" else "REVISION REQUESTED", color = Pink, fontWeight = FontWeight.Black)
                Text("Reason:", color = Muted, fontSize = 12.sp, modifier = Modifier.padding(top = 6.dp))
                Text("\"${lastReview.optString("feedback")}\"", color = Color.White, fontSize = 15.sp)
                Text("ACTION REQUIRED: RESUBMIT", color = Amber, fontWeight = FontWeight.Bold, fontSize = 13.sp, modifier = Modifier.padding(top = 8.dp))
            }
        }
        Section("Briefing", m.optString("description"))
        Section("Instructions", m.optString("instructions"))
        Section("Expected deliverables", m.optString("deliverables"))
        Section("How to submit", m.optString("submission_instructions"))
        val fmts = m.optJSONArray("allowed_formats").strings()
        Section("Allowed files", (if (fmts.isEmpty()) "Any format" else fmts.joinToString(", ") { ".$it" }) + " · max ${m.optInt("max_attachment_mb")} MB each")
        val refs = m.optJSONArray("reference_files").objs()
        if (refs.isNotEmpty()) {
            Label("Reference files")
            refs.forEach { FileRow(it) }
        }
        Spacer(Modifier.height(18.dp))
        when (status) {
            "assigned" -> BigButton("⚡ ACCEPT MISSION") { PbStore.accept(assignmentId) }
            "in_progress", "revision_required" -> if (a.optInt("attempts_left") > 0) BigButton(if (subs.isEmpty()) "🚀 SUBMIT MISSION" else "🔁 RESUBMIT (attempt ${subs.size + 1})") { submitting = true }
            "submitted" -> Banner("⏳ SUBMISSION RECEIVED – AWAITING COMMANDER REVIEW", Amber)
            "completed" -> Banner("✅ MISSION STATUS: VERIFIED", Green)
            "failed" -> Banner("No attempts left for this mission", Red)
        }
        val pending = PbStore.outbox.collectAsState().value.filter { it.optString("assignment_id") == assignmentId }
        pending.forEach { Banner(it.optString("failed").ifBlank { "📤 Submission queued — uploading / waiting for connection…" }, if (it.has("failed")) Red else Cyan) }
        if (subs.isNotEmpty()) {
            Spacer(Modifier.height(18.dp))
            Label("Submission history")
            subs.reversed().forEach { s ->
                Column(Modifier.fillMaxWidth().padding(vertical = 5.dp).clip(RoundedCornerShape(14.dp)).background(Card).padding(12.dp)) {
                    Row {
                        Text("Attempt ${s.optInt("attempt")}", color = Color.White, fontWeight = FontWeight.Bold)
                        Spacer(Modifier.weight(1f))
                        val st = s.optString("status")
                        Chip(when (st) { "pending" -> "UNDER REVIEW"; "approved" -> "APPROVED"; "rejected" -> "REJECTED"; else -> "REVISION" },
                            when (st) { "approved" -> Green; "pending" -> Amber; else -> Pink })
                    }
                    Text(fmtTime(s.optDouble("submitted_at")), color = Muted, fontSize = 11.sp)
                    if (s.optString("text").isNotBlank()) Text(s.optString("text"), color = Color(0xFFCBD5E1), fontSize = 13.sp, modifier = Modifier.padding(top = 4.dp))
                    s.optJSONArray("attachments").objs().forEach { FileRow(it) }
                    s.optJSONArray("reviews").objs().forEach { r ->
                        Text("Commander: ${r.optString("feedback").ifBlank { "Approved" }}", color = if (r.optString("action") == "approve") Green else Pink,
                            fontSize = 12.sp, modifier = Modifier.padding(top = 4.dp))
                    }
                }
            }
        }
        Spacer(Modifier.height(30.dp))
    }
}

/** Shared attachment picker row: gallery, camera photo/video, document, voice. Camera/mic only after the person taps. */
@Composable
private fun AttachBar(purpose: String, assignmentId: String = "", compact: Boolean = false, onAdded: (String) -> Unit) {
    val ctx = LocalContext.current
    fun add(uri: Uri, mime: String? = null) = PbStore.addUpload(ctx, uri, purpose, assignmentId, mime) { onAdded(it.uploadId) }
    val gallery = rememberLauncherForActivityResult(ActivityResultContracts.GetMultipleContents()) { it.forEach { u -> add(u) } }
    val docs = rememberLauncherForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { it.forEach { u -> add(u) } }
    var pendingCapture by remember { mutableStateOf<Pair<Uri, File>?>(null) }
    val photo = rememberLauncherForActivityResult(ActivityResultContracts.TakePicture()) { ok -> pendingCapture?.let { if (ok) add(Uri.fromFile(it.second), "image/jpeg") }; pendingCapture = null }
    val video = rememberLauncherForActivityResult(ActivityResultContracts.CaptureVideo()) { ok -> pendingCapture?.let { if (ok) add(Uri.fromFile(it.second), "video/mp4") }; pendingCapture = null }
    var afterCamPerm by remember { mutableStateOf<(() -> Unit)?>(null) }
    val camPerm = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { ok ->
        if (ok) afterCamPerm?.invoke() else PbStore.error.value = "Camera permission is needed to capture"
    }
    fun withCamera(block: () -> Unit) { if (granted(ctx, Manifest.permission.CAMERA)) block() else { afterCamPerm = block; camPerm.launch(Manifest.permission.CAMERA) } }

    // voice recording (visible, user-started, with a stop button)
    var recorder by remember { mutableStateOf<MediaRecorder?>(null) }
    var recFile by remember { mutableStateOf<File?>(null) }
    val micPerm = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { ok -> if (!ok) PbStore.error.value = "Microphone permission is needed for voice messages" }
    fun startRec() {
        if (!granted(ctx, Manifest.permission.RECORD_AUDIO)) { micPerm.launch(Manifest.permission.RECORD_AUDIO); return }
        runCatching {
            val f = File(File(ctx.cacheDir, "pb_capture").apply { mkdirs() }, "voice_${System.currentTimeMillis()}.m4a")
            @Suppress("DEPRECATION")
            val r = if (Build.VERSION.SDK_INT >= 31) MediaRecorder(ctx) else MediaRecorder()
            r.setAudioSource(MediaRecorder.AudioSource.MIC); r.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
            r.setAudioEncoder(MediaRecorder.AudioEncoder.AAC); r.setAudioEncodingBitRate(64000); r.setAudioSamplingRate(44100)
            r.setOutputFile(f.absolutePath); r.prepare(); r.start()
            recorder = r; recFile = f
        }.onFailure { PbStore.error.value = "Could not start recording"; PbStore.log("error", "voice", it.toString()) }
    }
    fun stopRec() {
        val r = recorder ?: return
        recorder = null
        val ok = runCatching { r.stop() }.isSuccess
        runCatching { r.release() }
        recFile?.let { if (ok && it.length() > 0) add(Uri.fromFile(it), "audio/mp4") }
    }
    DisposableEffect(Unit) { onDispose { recorder?.let { runCatching { it.stop() }; runCatching { it.release() } } } }

    Row(Modifier.horizontalScroll(rememberScrollState()), verticalAlignment = Alignment.CenterVertically) {
        if (recorder != null) {
            val t = rememberInfiniteTransition(label = "rec")
            val a by t.animateFloat(0.3f, 1f, infiniteRepeatable(tween(600), RepeatMode.Reverse), label = "r")
            Pill("⏺ Recording… tap to stop", Red.copy(alpha = a)) { stopRec() }
            return@Row
        }
        Pill("🖼 Gallery") { gallery.launch("*/*") }
        Pill("📷 Photo") { withCamera { captureUri(ctx, "jpg").also { pendingCapture = it; photo.launch(it.first) } } }
        Pill("🎥 Video") { withCamera { captureUri(ctx, "mp4").also { pendingCapture = it; video.launch(it.first) } } }
        Pill("📄 File") { docs.launch(arrayOf("*/*")) }
        Pill("🎙 Voice") { startRec() }
    }
}

@Composable
private fun UploadChips(ids: List<String>, onRemove: (String) -> Unit) {
    val ups by PbStore.uploads.collectAsState()
    Column {
        ids.mapNotNull { ups[it] }.forEach { u ->
            Row(Modifier.fillMaxWidth().padding(vertical = 3.dp).clip(RoundedCornerShape(12.dp)).background(Card2).padding(10.dp),
                verticalAlignment = Alignment.CenterVertically) {
                Text(kindIcon(u.kind), fontSize = 18.sp)
                Column(Modifier.weight(1f).padding(horizontal = 8.dp)) {
                    Text(u.name, color = Color.White, fontSize = 13.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
                    val pct = if (u.size > 0) u.received.toFloat() / u.size else 0f
                    when (u.status) {
                        "ready" -> Text("✓ uploaded · ${human(u.size)}", color = Green, fontSize = 11.sp)
                        "error" -> Text("⚠ ${u.error} — tap ↻", color = Pink, fontSize = 11.sp)
                        else -> {
                            LinearProgressIndicator(progress = { pct }, Modifier.fillMaxWidth().padding(top = 4.dp).height(4.dp).clip(RoundedCornerShape(2.dp)), color = Cyan, trackColor = Color.White.copy(alpha = 0.1f))
                            Text("${(pct * 100).toInt()}% of ${human(u.size)}", color = Muted, fontSize = 10.sp)
                        }
                    }
                }
                if (u.status == "error") Text("↻", color = Cyan, fontSize = 20.sp, modifier = Modifier.clickable { PbStore.retryUpload(u.uploadId) }.padding(6.dp))
                Text("✕", color = Muted, modifier = Modifier.clickable { onRemove(u.uploadId) }.padding(6.dp))
            }
        }
    }
}

@Composable
private fun SubmitForm(a: JSONObject, onDone: () -> Unit) {
    val m = a.getJSONObject("mission")
    var text by remember { mutableStateOf("") }
    var comment by remember { mutableStateOf("") }
    val ids = remember { mutableStateListOf<String>() }
    val ups by PbStore.uploads.collectAsState()
    BackHandler { onDone() }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp)) {
        Text("SUBMIT MISSION", color = Color.White, fontSize = 22.sp, fontWeight = FontWeight.Black)
        Text("${m.optString("title")} · attempt ${a.optJSONArray("submissions").objs().size + 1} of ${m.optInt("max_attempts")}", color = Muted, fontSize = 13.sp)
        if (m.optString("submission_instructions").isNotBlank())
            Text(m.optString("submission_instructions"), color = Cyan, fontSize = 13.sp, modifier = Modifier.padding(top = 8.dp))
        Spacer(Modifier.height(14.dp))
        Field(text, { text = it }, "Describe what you did", minLines = 4)
        Spacer(Modifier.height(10.dp))
        Field(comment, { comment = it }, "Comments for the Commander (optional)", minLines = 2)
        Spacer(Modifier.height(14.dp))
        Label("Evidence")
        AttachBar("submission", a.optString("id")) { ids.add(it) }
        UploadChips(ids) { ids.remove(it); PbStore.removeUpload(it) }
        Spacer(Modifier.height(20.dp))
        val busy = ids.any { ups[it]?.status != "ready" }
        BigButton(if (busy) "🚀 SUBMIT (sends when uploads finish)" else "🚀 SUBMIT FOR REVIEW", enabled = text.isNotBlank() || ids.isNotEmpty()) {
            PbStore.submit(a.optString("id"), text, comment, ids.toList())
            onDone()
        }
        Text("Your work is saved on the phone and sent automatically, even if the connection drops.", color = Muted, fontSize = 11.sp,
            modifier = Modifier.padding(top = 8.dp))
    }
}

// ---------------- Chat ----------------
@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun ChatTab() {
    val msgs by PbStore.messages.collectAsState()
    val outbox by PbStore.outbox.collectAsState()
    val typing by PbStore.commanderTyping.collectAsState()
    val adminRead by PbStore.adminReadSeq.collectAsState()
    var input by remember { mutableStateOf("") }
    var replyTo by remember { mutableStateOf<JSONObject?>(null) }
    var menuFor by remember { mutableStateOf<JSONObject?>(null) }
    var search by remember { mutableStateOf<String?>(null) }
    var showAttach by remember { mutableStateOf(false) }
    val attach = remember { mutableStateListOf<String>() }
    val state = rememberLazyListState()
    DisposableEffect(Unit) { PbStore.chatVisible = true; onDispose { PbStore.chatVisible = false } }
    LaunchedEffect(msgs.size) { PbStore.markRead(); if (msgs.isNotEmpty()) state.animateScrollToItem(msgs.size + outbox.size) }

    val shown = search?.takeIf { it.isNotBlank() }?.let { q -> msgs.filter { it.optString("text").contains(q, true) } } ?: msgs
    // Mood-reactive theme: detect the mood from recent messages, then keep cycling that mood's palettes
    val mood = remember(msgs) { ChatMood.detect(msgs) }
    var palIdx by remember(mood) { mutableStateOf(0) }
    LaunchedEffect(mood) { while (true) { kotlinx.coroutines.delay(15000); palIdx++ } }   // drift slowly
    val pals = ChatMood.PALETTES.getValue(mood); val pal = pals[palIdx % pals.size]
    val spec = tween<Color>(4000)
    val moodA by animateColorAsState(pal.a, spec, label = "moodA")
    val moodB by animateColorAsState(pal.b, spec, label = "moodB")
    val moodAccent by animateColorAsState(pal.accent, spec, label = "moodAccent")
    val moodGrad = Brush.linearGradient(listOf(moodA, moodB))
    // The whole chat is tinted from the accent over a soft, non-black base (no flashing; easy on the eyes)
    val base = Color(0xFF121426)
    val cBg = lerp(base, moodAccent, 0.07f); val cSurface = lerp(base, moodAccent, 0.12f)
    val cTheirs = lerp(Color(0xFF1B1E33), moodAccent, 0.18f); val cText = Color(0xFFE4E6EE); val cMuted = Color(0xFFA2A7BD)
    Column(Modifier.fillMaxSize().background(cBg)) {
        Row(Modifier.fillMaxWidth().background(cSurface).padding(horizontal = 14.dp, vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(40.dp).clip(CircleShape).background(moodGrad), contentAlignment = Alignment.Center) { Text("★", color = Color.White, fontSize = 18.sp) }
            Column(Modifier.weight(1f).padding(start = 10.dp)) {
                Text("Commander", color = cText, fontWeight = FontWeight.Bold)
                Text(if (typing) "typing…" else "Private channel · only you and HQ", color = if (typing) moodAccent else cMuted, fontSize = 11.sp)
            }
            Text(ChatMood.NAMES.getValue(mood), color = cText, fontSize = 11.sp, fontWeight = FontWeight.SemiBold,
                modifier = Modifier.clip(RoundedCornerShape(50)).background(moodAccent.copy(alpha = 0.22f))
                    .border(1.dp, moodAccent.copy(alpha = 0.4f), RoundedCornerShape(50)).padding(horizontal = 8.dp, vertical = 3.dp))
            Text("🔍", fontSize = 18.sp, modifier = Modifier.clickable { search = if (search == null) "" else null }.padding(8.dp))
        }
        if (search != null) Field(search!!, { search = it }, "Search messages", modifier = Modifier.padding(horizontal = 14.dp))
        LazyColumn(Modifier.weight(1f).fillMaxWidth(), state = state, contentPadding = PaddingValues(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            if (shown.isEmpty()) item { Empty("💬", "Say hi to HQ", "Messages to the Commander are private to this phone.") }
            items(shown, key = { it.optString("id") }) { m ->
                val mine = m.optString("sender") == "member"
                Bubble(m, mine, seen = mine && adminRead >= m.optLong("seq") && m == msgs.lastOrNull { it.optString("sender") == "member" },
                    modifier = Modifier.combinedClickable(onClick = {}, onLongClick = { menuFor = m }), mineBrush = moodGrad,
                    theirs = cTheirs, text = cText, muted = cMuted, chip = cSurface)
            }
            items(outbox.filter { it.optString("kind") == "msg" }, key = { it.optString("client_id") }) { o ->
                Box(Modifier.fillMaxWidth(), contentAlignment = Alignment.CenterEnd) {
                    Column(Modifier.widthIn(max = 280.dp).clip(RoundedCornerShape(18.dp)).background(moodA.copy(alpha = 0.5f)).padding(12.dp)) {
                        if (o.optString("text").isNotBlank()) Text(o.optString("text"), color = Color.White)
                        UploadChips(o.optJSONArray("uploads").strings()) {}
                        Text("sending…", color = Color.White.copy(alpha = 0.6f), fontSize = 10.sp)
                    }
                }
            }
        }
        menuFor?.let { m ->
            Row(Modifier.fillMaxWidth().background(cSurface).padding(8.dp), verticalAlignment = Alignment.CenterVertically) {
                listOf("❤️", "🔥", "👍", "😂", "😮").forEach { e -> Text(e, fontSize = 22.sp, modifier = Modifier.clickable { PbStore.react(m.optString("id"), e); menuFor = null }.padding(6.dp)) }
                Spacer(Modifier.weight(1f))
                Text("Reply", color = moodAccent, fontWeight = FontWeight.Bold, modifier = Modifier.clickable { replyTo = m; menuFor = null }.padding(8.dp))
                Text("✕", color = Muted, modifier = Modifier.clickable { menuFor = null }.padding(8.dp))
            }
        }
        replyTo?.let { r ->
            Row(Modifier.fillMaxWidth().background(cSurface).padding(10.dp)) {
                Box(Modifier.width(3.dp).height(30.dp).background(moodAccent))
                Text("Replying: ${r.optString("text").ifBlank { "attachment" }}", color = Muted, fontSize = 12.sp, maxLines = 1, modifier = Modifier.weight(1f).padding(start = 8.dp))
                Text("✕", color = Muted, modifier = Modifier.clickable { replyTo = null })
            }
        }
        if (showAttach || attach.isNotEmpty()) Column(Modifier.background(cSurface).padding(8.dp)) {
            AttachBar("chat") { attach.add(it) }
            UploadChips(attach) { attach.remove(it); PbStore.removeUpload(it) }
        }
        Row(Modifier.fillMaxWidth().background(cSurface).padding(8.dp), verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(42.dp).clip(CircleShape).background(if (showAttach) moodA else cTheirs).clickable { showAttach = !showAttach }, contentAlignment = Alignment.Center) {
                Text("+", color = Color.White, fontSize = 22.sp)
            }
            Field(input, { input = it; PbStore.typing() }, "Message…", modifier = Modifier.weight(1f).padding(horizontal = 8.dp))
            Box(Modifier.size(42.dp).clip(CircleShape).background(moodGrad).clickable {
                if (input.isNotBlank() || attach.isNotEmpty()) {
                    PbStore.sendMessage(input.trim(), replyTo?.optString("id") ?: "", attach.toList())
                    input = ""; replyTo = null; attach.clear(); showAttach = false
                }
            }, contentAlignment = Alignment.Center) { Text("➤", color = Color.White, fontSize = 18.sp) }
        }
    }
}

@Composable
private fun Bubble(m: JSONObject, mine: Boolean, seen: Boolean, modifier: Modifier, mineBrush: Brush = Grad,
                   theirs: Color = Card2, text: Color = Color.White, muted: Color = Muted, chip: Color = Card) {
    Box(Modifier.fillMaxWidth(), contentAlignment = if (mine) Alignment.CenterEnd else Alignment.CenterStart) {
        Column(horizontalAlignment = if (mine) Alignment.End else Alignment.Start) {
            Column(modifier.widthIn(max = 290.dp).clip(RoundedCornerShape(20.dp, 20.dp, if (mine) 4.dp else 20.dp, if (mine) 20.dp else 4.dp))
                .background(if (mine) mineBrush else Brush.linearGradient(listOf(theirs, theirs))).padding(12.dp)) {
                m.optJSONObject("reply")?.let { r ->
                    Text("↩ ${r.optString("text").ifBlank { "attachment" }}", color = Color.White.copy(alpha = 0.7f), fontSize = 11.sp, maxLines = 2,
                        modifier = Modifier.clip(RoundedCornerShape(8.dp)).background(Color.Black.copy(alpha = 0.2f)).padding(6.dp))
                    Spacer(Modifier.height(4.dp))
                }
                if (m.optInt("forwarded") == 1) Text("↪ Forwarded", color = Color.White.copy(alpha = 0.6f), fontSize = 10.sp)
                m.optJSONArray("attachments").objs().forEach { FileRow(it, inBubble = true) }
                if (m.optString("text").isNotBlank()) Text(m.optString("text"), color = if (mine) Color(0xFFF7F7FB) else text, fontSize = 15.sp, lineHeight = 21.sp)
                Text(fmtTime(m.optDouble("created_at")), color = (if (mine) Color.White else text).copy(alpha = 0.6f), fontSize = 9.sp, modifier = Modifier.align(Alignment.End))
            }
            val reacts = m.optJSONObject("reactions")
            if (reacts != null && reacts.length() > 0) Text(reacts.keys().asSequence().map { reacts.optString(it) }.joinToString(""),
                fontSize = 14.sp, modifier = Modifier.offset(y = (-4).dp).clip(RoundedCornerShape(10.dp)).background(chip).padding(horizontal = 6.dp))
            if (seen) Text("Seen", color = muted, fontSize = 10.sp)
        }
    }
}

private fun kindIcon(k: String) = when (k) { "image" -> "🖼"; "video" -> "🎬"; "audio" -> "🎵"; else -> "📄" }
private fun human(b: Long) = when { b > 1 shl 20 -> "%.1f MB".format(b / 1048576.0); b > 1024 -> "${b / 1024} KB"; else -> "$b B" }

/** An attachment from the laptop: downloads on demand (authorised by HQ), image previews inline, others open in an app. */
@Composable
private fun FileRow(f: JSONObject, inBubble: Boolean = false) {
    val ctx = LocalContext.current
    val id = f.optString("id")
    val dl by PbStore.downloads.collectAsState()
    val prog by PbStore.downloadProgress.collectAsState()
    val local = dl[id]?.let { File(it) }?.takeIf { it.exists() }
    val kind = f.optString("kind")
    LaunchedEffect(id) { if (kind == "image" && f.optLong("size") < 15_000_000) PbStore.download(ctx, id, f.optString("name")) }
    if (kind == "image" && local != null) {
        val bmp = remember(local.path) {
            runCatching {
                val o = BitmapFactory.Options().apply { inJustDecodeBounds = true }
                BitmapFactory.decodeFile(local.path, o)
                var s = 1; while (o.outWidth / s > 900) s *= 2
                BitmapFactory.decodeFile(local.path, BitmapFactory.Options().apply { inSampleSize = s })?.asImageBitmap()
            }.getOrNull()
        }
        if (bmp != null) {
            Image(bmp, f.optString("name"), contentScale = ContentScale.Crop,
                modifier = Modifier.padding(vertical = 4.dp).fillMaxWidth().heightIn(max = 240.dp).clip(RoundedCornerShape(14.dp)).clickable { openFile(ctx, local, f.optString("mime")) })
            return
        }
    }
    Row(Modifier.fillMaxWidth().padding(vertical = 3.dp).clip(RoundedCornerShape(12.dp)).background(if (inBubble) Color.Black.copy(alpha = 0.2f) else Card2)
        .clickable { if (local != null) openFile(ctx, local, f.optString("mime")) else PbStore.download(ctx, id, f.optString("name")) }.padding(10.dp),
        verticalAlignment = Alignment.CenterVertically) {
        Text(kindIcon(kind), fontSize = 20.sp)
        Column(Modifier.weight(1f).padding(start = 8.dp)) {
            Text(f.optString("name"), color = Color.White, fontSize = 13.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
            val p = prog[id]
            Text(when { local != null -> "Tap to open · ${human(f.optLong("size"))}"; p != null -> "Downloading ${(p * 100).toInt()}%"; else -> "Tap to download · ${human(f.optLong("size"))}" },
                color = Muted, fontSize = 11.sp)
        }
    }
}

// ---------------- Squad / profile ----------------
@Composable
private fun SquadTab() {
    val p by PbStore.profile.collectAsState()
    val lb by PbStore.leaderboard.collectAsState()
    var period by remember { mutableStateOf("global") }
    val prof = p ?: run { Empty("🏆", "Connect to HQ", "Your profile loads the first time this phone reaches the laptop."); return }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp)) {
        Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(22.dp)).background(Brush.linearGradient(listOf(Color(0xFF2A1B5E), Color(0xFF13245A)))).padding(18.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.size(64.dp).clip(CircleShape).background(Grad).border(2.dp, Cyan, CircleShape), contentAlignment = Alignment.Center) {
                    Text("${prof.optInt("level", 1)}", color = Color.White, fontSize = 26.sp, fontWeight = FontWeight.Black)
                }
                Column(Modifier.padding(start = 14.dp)) {
                    Text(PbStore.username.collectAsState().value ?: prof.optString("name"), color = Color.White, fontSize = 19.sp, fontWeight = FontWeight.Bold)
                    Text("Level ${prof.optInt("level", 1)} Operator · ${prof.optInt("xp")} XP", color = Cyan, fontSize = 13.sp)
                }
            }
            Spacer(Modifier.height(14.dp))
            val floor = prof.optInt("level_floor"); val next = prof.optInt("next_level_xp", 100)
            val pct = ((prof.optInt("xp") - floor).toFloat() / (next - floor).coerceAtLeast(1)).coerceIn(0f, 1f)
            val anim by animateFloatAsState(pct, tween(900), label = "xp")
            Box(Modifier.fillMaxWidth().height(12.dp).clip(RoundedCornerShape(6.dp)).background(Color.White.copy(alpha = 0.1f))) {
                Box(Modifier.fillMaxHeight().fillMaxWidth(anim).clip(RoundedCornerShape(6.dp)).background(Brush.horizontalGradient(listOf(Cyan, Purple, Pink))))
            }
            Text("${prof.optInt("xp_to_next")} XP to level ${prof.optInt("level", 1) + 1}", color = Muted, fontSize = 11.sp, modifier = Modifier.padding(top = 4.dp))
            Spacer(Modifier.height(12.dp))
            Row {
                Stat("${prof.optInt("completed_missions")}", "Completed", Modifier.weight(1f))
                Stat("${prof.optInt("assigned_missions")}", "Assigned", Modifier.weight(1f))
                Stat("${prof.optInt("rejected_attempts")}", "Revisions", Modifier.weight(1f))
            }
        }
        Spacer(Modifier.height(16.dp))
        Label("Achievements")
        val badges = prof.optJSONArray("achievements").objs()
        badges.chunked(3).forEach { row ->
            Row(Modifier.fillMaxWidth()) {
                row.forEach { b ->
                    val on = !b.isNull("unlocked_at")
                    Column(Modifier.weight(1f).padding(4.dp).clip(RoundedCornerShape(14.dp)).background(if (on) Card2 else Card).padding(10.dp),
                        horizontalAlignment = Alignment.CenterHorizontally) {
                        Text(if (on) b.optString("icon") else "🔒", fontSize = 26.sp)
                        Text(b.optString("name"), color = if (on) Color.White else Muted, fontSize = 11.sp, textAlign = TextAlign.Center, fontWeight = FontWeight.SemiBold)
                    }
                }
                repeat(3 - row.size) { Spacer(Modifier.weight(1f)) }
            }
        }
        Spacer(Modifier.height(16.dp))
        Label("Leaderboard")
        Row {
            listOf("global" to "All-time", "weekly" to "Weekly", "monthly" to "Monthly").forEach { (k, l) ->
                Pill(l, if (period == k) Purple else Card2) { period = k }
            }
        }
        var lastRank = 0
        lb?.optJSONArray(period).objs().forEach { r ->
            val me = r.optString("device_id") == prof.optString("device_id")
            if (r.optInt("rank") > lastRank + 1) Text("⋯", color = Muted, fontSize = 18.sp, modifier = Modifier.fillMaxWidth(), textAlign = TextAlign.Center)
            lastRank = r.optInt("rank")
            Row(Modifier.fillMaxWidth().padding(vertical = 3.dp).clip(RoundedCornerShape(12.dp)).background(if (me) Purple.copy(alpha = 0.35f) else Card).padding(12.dp),
                verticalAlignment = Alignment.CenterVertically) {
                Text(when (r.optInt("rank")) { 1 -> "🥇"; 2 -> "🥈"; 3 -> "🥉"; else -> "#${r.optInt("rank")}" }, color = Color.White, fontSize = 13.sp, maxLines = 1, softWrap = false, modifier = Modifier.width(52.dp))
                Text(r.optString("name") + if (me) " (you)" else "", color = Color.White, modifier = Modifier.weight(1f), maxLines = 1)
                Text("${r.optInt("xp")} XP", color = Amber, fontWeight = FontWeight.Bold)
            }
        }
        Spacer(Modifier.height(16.dp))
        Label("XP history")
        prof.optJSONArray("activity").objs().forEach { t ->
            Row(Modifier.fillMaxWidth().padding(vertical = 4.dp)) {
                Text(t.optString("reason"), color = Color(0xFFCBD5E1), fontSize = 13.sp, modifier = Modifier.weight(1f))
                Text("+${t.optInt("amount")}", color = Green, fontWeight = FontWeight.Bold)
            }
        }
        Spacer(Modifier.height(24.dp))
    }
}

@Composable
private fun AlertsTab() {
    val list by PbStore.notifications.collectAsState()
    if (list.isEmpty()) { Empty("🔔", "All quiet", "Mission drops, reviews, XP and badges appear here."); return }
    LazyColumn(contentPadding = PaddingValues(14.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        items(list, key = { it.optLong("seq") }) { n ->
            Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(14.dp)).background(if (n.optInt("read") == 0) Card2 else Card).padding(12.dp)) {
                Text(n.optString("title"), color = Color.White, fontWeight = FontWeight.Bold)
                if (n.optString("body").isNotBlank()) Text(n.optString("body"), color = Muted, fontSize = 13.sp)
                Text(fmtTime(n.optDouble("created_at")), color = Muted, fontSize = 10.sp)
            }
        }
    }
}

// ---------------- overlays ----------------
@Composable
private fun RewardOverlay() {
    val r by PbStore.reward.collectAsState()
    AnimatedVisibility(r != null, enter = fadeIn() + scaleIn(initialScale = 0.6f), exit = fadeOut()) {
        val rw = r ?: return@AnimatedVisibility
        val t = rememberInfiniteTransition(label = "win")
        val glow by t.animateFloat(0.9f, 1.12f, infiniteRepeatable(tween(700, easing = FastOutSlowInEasing), RepeatMode.Reverse), label = "g")
        Box(Modifier.fillMaxSize().background(Color(0xF2050614)).clickable { PbStore.reward.value = null }, contentAlignment = Alignment.Center) {
            Column(horizontalAlignment = Alignment.CenterHorizontally, modifier = Modifier.padding(24.dp)) {
                Text("🏆", fontSize = 90.sp, modifier = Modifier.scale(glow))
                Text("MISSION COMPLETED!", color = Color.White, fontSize = 28.sp, fontWeight = FontWeight.Black, letterSpacing = 1.sp)
                Text("+${rw.optInt("xp")} XP EARNED", color = Amber, fontSize = 34.sp, fontWeight = FontWeight.Black, modifier = Modifier.padding(top = 10.dp).scale(glow))
                Text("MISSION STATUS: VERIFIED", color = Green, fontSize = 15.sp, fontWeight = FontWeight.Bold, modifier = Modifier.padding(top = 8.dp))
                Text(rw.optString("title"), color = Muted, fontSize = 13.sp, textAlign = TextAlign.Center, modifier = Modifier.padding(top = 6.dp))
                rw.optJSONArray("unlocked").objs().forEach { b ->
                    Text("${b.optString("icon")} Achievement unlocked: ${b.optString("name")}", color = Cyan, fontWeight = FontWeight.Bold, modifier = Modifier.padding(top = 10.dp))
                }
                Spacer(Modifier.height(24.dp))
                Text("Tap to continue", color = Muted, fontSize = 12.sp)
            }
        }
    }
}

@Composable
private fun SubmittedOverlay() {
    val s by PbStore.submitted.collectAsState()
    if (s == null) return
    LaunchedEffect(s) { kotlinx.coroutines.delay(3500); PbStore.submitted.value = null }
    Box(Modifier.fillMaxSize().background(Color(0xF2050614)).clickable { PbStore.submitted.value = null }, contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally, modifier = Modifier.padding(28.dp)) {
            Text("📡", fontSize = 70.sp)
            Text("SUBMISSION RECEIVED", color = Color.White, fontSize = 24.sp, fontWeight = FontWeight.Black)
            Text("AWAITING COMMANDER REVIEW", color = Amber, fontSize = 16.sp, fontWeight = FontWeight.Bold)
            Text("Attempt ${s!!.optInt("attempt")}", color = Muted, modifier = Modifier.padding(top = 6.dp))
        }
    }
}

// ---------------- small parts ----------------
@Composable private fun Chip(t: String, c: Color) =
    Text(t, color = c, fontSize = 10.sp, fontWeight = FontWeight.Black, letterSpacing = 1.sp,
        modifier = Modifier.clip(RoundedCornerShape(8.dp)).background(c.copy(alpha = 0.15f)).padding(horizontal = 8.dp, vertical = 3.dp))

@Composable private fun Label(t: String) = Text(t.uppercase(), color = Muted, fontSize = 11.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.5.sp,
    modifier = Modifier.padding(top = 14.dp, bottom = 6.dp))

@Composable private fun Section(title: String, body: String) {
    if (body.isBlank()) return
    Label(title)
    Text(body, color = Color(0xFFE2E8F0), fontSize = 14.sp)
}

@Composable private fun Stat(v: String, l: String, modifier: Modifier) = Column(modifier, horizontalAlignment = Alignment.CenterHorizontally) {
    Text(v, color = Color.White, fontSize = 20.sp, fontWeight = FontWeight.Black); Text(l, color = Muted, fontSize = 11.sp)
}

@Composable private fun Banner(t: String, c: Color) =
    Text(t, color = c, fontWeight = FontWeight.Bold, fontSize = 14.sp, textAlign = TextAlign.Center,
        modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp).clip(RoundedCornerShape(14.dp)).background(c.copy(alpha = 0.12f)).border(1.dp, c.copy(alpha = 0.5f), RoundedCornerShape(14.dp)).padding(14.dp))

@Composable private fun Pill(t: String, bg: Color = Card2, onClick: () -> Unit) =
    Text(t, color = Color.White, fontSize = 13.sp, fontWeight = FontWeight.SemiBold,
        modifier = Modifier.padding(end = 6.dp, bottom = 4.dp).clip(RoundedCornerShape(18.dp)).background(bg).clickable(onClick = onClick).padding(horizontal = 12.dp, vertical = 8.dp))

@Composable private fun BigButton(t: String, enabled: Boolean = true, onClick: () -> Unit) =
    Box(Modifier.fillMaxWidth().height(56.dp).clip(RoundedCornerShape(16.dp)).background(if (enabled) Grad else Brush.linearGradient(listOf(Card2, Card2)))
        .clickable(enabled = enabled, onClick = onClick), contentAlignment = Alignment.Center) {
        Text(t, color = Color.White, fontWeight = FontWeight.Black, fontSize = 16.sp, letterSpacing = 1.sp)
    }

@Composable private fun Field(v: String, on: (String) -> Unit, hint: String, minLines: Int = 1, modifier: Modifier = Modifier.fillMaxWidth()) =
    OutlinedTextField(v, on, modifier, placeholder = { Text(hint, color = Muted) }, minLines = minLines, shape = RoundedCornerShape(16.dp),
        colors = OutlinedTextFieldDefaults.colors(focusedTextColor = Color.White, unfocusedTextColor = Color.White, focusedBorderColor = Purple,
            unfocusedBorderColor = Card2, focusedContainerColor = Card, unfocusedContainerColor = Card, cursorColor = Cyan))

@Composable private fun Empty(icon: String, title: String, body: String) =
    Column(Modifier.fillMaxWidth().padding(40.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        Text(icon, fontSize = 54.sp); Text(title, color = Color.White, fontWeight = FontWeight.Bold, fontSize = 18.sp, modifier = Modifier.padding(top = 8.dp))
        Text(body, color = Muted, fontSize = 13.sp, textAlign = TextAlign.Center, modifier = Modifier.padding(top = 4.dp))
    }

/** First start: the player picks the name the squad and leaderboard will know them by. */
@Composable
fun UsernameScreen() {
    val ctx = LocalContext.current
    var name by remember { mutableStateOf("") }
    val err by PbStore.usernameError.collectAsState()
    val t = rememberInfiniteTransition(label = "un")
    val pulse by t.animateFloat(1f, 1.08f, infiniteRepeatable(tween(1000), RepeatMode.Reverse), label = "p")
    Column(Modifier.fillMaxSize().background(Brush.verticalGradient(listOf(Color(0xFF1A1442), Bg))).padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.Center) {
        Text("🎮", fontSize = 64.sp, modifier = Modifier.scale(pulse))
        Text("CHOOSE YOUR CALLSIGN", color = Color.White, fontSize = 24.sp, fontWeight = FontWeight.Black, letterSpacing = 1.sp, textAlign = TextAlign.Center)
        Text("This is how the squad and the leaderboard will know you.", color = Muted, fontSize = 13.sp, textAlign = TextAlign.Center,
            modifier = Modifier.padding(top = 6.dp, bottom = 22.dp))
        Field(name, { name = it.take(20).filter { c -> c.isLetterOrDigit() || c == '_' || c == '.' }; PbStore.usernameError.value = null }, "e.g. shadow_miner")
        Text(err ?: "3–20 letters, numbers, _ or .", color = if (err != null) Pink else Muted, fontSize = 12.sp,
            modifier = Modifier.fillMaxWidth().padding(top = 6.dp, start = 4.dp))
        Spacer(Modifier.height(22.dp))
        BigButton("⛏ ENTER THE MINES", enabled = name.length >= 3) { PbStore.chooseUsername(ctx, name) }
    }
}

/** Entry button shown on the game screen. */
@Composable
fun PowerBoostersEntry(modifier: Modifier = Modifier, onOpen: () -> Unit) {
    val ctx = LocalContext.current
    LaunchedEffect(Unit) { PbStore.init(ctx); while (true) { PbStore.refreshOnline(); kotlinx.coroutines.delay(2000) } }
    val alerts = PbStore.notifications.collectAsState().value.count { it.optInt("read") == 0 }
    val missions = PbStore.assignments.collectAsState().value.count { it.optString("status") in setOf("assigned", "in_progress", "revision_required") }
    val online by PbStore.online.collectAsState()
    val t = rememberInfiniteTransition(label = "pbe")
    val glow by t.animateFloat(0.55f, 1f, infiniteRepeatable(tween(1100), RepeatMode.Reverse), label = "g")
    Row(modifier.clip(RoundedCornerShape(18.dp)).background(Grad).border(1.dp, Cyan.copy(alpha = glow * 0.6f), RoundedCornerShape(18.dp))
        .clickable(onClick = onOpen).padding(horizontal = 14.dp, vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
        Text("⚡", fontSize = 22.sp)
        Column(Modifier.weight(1f).padding(start = 10.dp)) {
            Text("POWERBOOSTERS", color = Color.White, fontWeight = FontWeight.Black, fontSize = 14.sp, letterSpacing = 1.5.sp)
            Text(if (missions > 0) "$missions active mission${if (missions > 1) "s" else ""} · chat with HQ" else "Missions · Squad chat · XP",
                color = Color.White.copy(alpha = 0.8f), fontSize = 11.sp)
        }
        Box(Modifier.size(8.dp).clip(CircleShape).background(if (online) Green else Muted))
        if (alerts > 0) Box(Modifier.padding(start = 8.dp).clip(CircleShape).background(Pink).padding(horizontal = 7.dp, vertical = 2.dp)) {
            Text("$alerts", color = Color.White, fontSize = 11.sp, fontWeight = FontWeight.Bold)
        }
        Text("  ›", color = Color.White, fontSize = 20.sp, fontWeight = FontWeight.Bold)
    }
}
