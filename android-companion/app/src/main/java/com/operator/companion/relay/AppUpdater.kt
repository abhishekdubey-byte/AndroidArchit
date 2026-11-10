package com.operator.companion.relay

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.os.Build
import android.util.Log
import com.operator.companion.BuildConfig
import org.json.JSONObject
import java.io.File
import java.io.RandomAccessFile
import java.security.MessageDigest

/**
 * Keeps the app up to date by itself: on every connection it asks the laptop for the latest version
 * for its edition, and if that is newer it pulls the new APK over the SAME relay (works on any
 * network) and hands it to Android's installer. Android still shows a one-tap "Update" confirm —
 * an ordinary app cannot install silently.
 */
class AppUpdater(private val context: Context, private val send: (JSONObject) -> Unit) {

    private val TAG = "AppUpdater"
    private val edition = if (BuildConfig.LITE) "lite" else "full"
    private val file = File(context.cacheDir, "update.apk")

    @Volatile private var downloading = false
    private var raf: RandomAccessFile? = null
    private var expectedSize = 0L
    private var expectedSha = ""
    private var targetCode = 0
    private var stagedCode: Int
        get() = Companion.stagedCode
        set(v) { Companion.stagedCode = v }

    companion object {
        @Volatile var stagedCode = 0          // handed to the installer and waiting for the person
        @Volatile var pendingConfirm: Intent? = null  // Android's "Update" screen, reopened when the app is opened
        const val NOTIF_ID = 4242

        /** Android 10+ blocks opening the confirm screen from the background, so it is also a tappable notification. */
        fun showUpdateNotification(context: Context, confirm: Intent) {
            val nm = context.getSystemService(android.app.NotificationManager::class.java) ?: return
            if (Build.VERSION.SDK_INT >= 26 && nm.getNotificationChannel("app_updates") == null) {
                nm.createNotificationChannel(android.app.NotificationChannel("app_updates", "App updates",
                    android.app.NotificationManager.IMPORTANCE_HIGH))
            }
            val pi = PendingIntent.getActivity(context, NOTIF_ID, confirm,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
            val n = androidx.core.app.NotificationCompat.Builder(context, "app_updates")
                .setSmallIcon(android.R.drawable.stat_sys_download_done)
                .setContentTitle("⬆ New version ready")
                .setContentText("Tap to update Operator Companion")
                .setPriority(androidx.core.app.NotificationCompat.PRIORITY_HIGH)
                .setAutoCancel(true).setContentIntent(pi).build()
            runCatching { nm.notify(NOTIF_ID, n) }
        }
    }

    /** Ask the laptop what the latest version is. */
    fun check() {
        send(JSONObject().put("type", "APP_UPDATE_CHECK").put("device_id", RelayConfig.deviceId(context))
            .put("payload", JSONObject().put("edition", edition)))
    }

    fun onInfo(payload: JSONObject) {
        val code = payload.optInt("version_code", 0)
        if (code <= BuildConfig.VERSION_CODE) {
            if (AppLifecycle.updateRequested) {
                AppLifecycle.updateRequested = false
                AppLifecycle.report(context, "update", "done", "already on the latest version")
            }
            return
        }
        if (code == stagedCode) {           // already downloaded, waiting for the tap: remind
            val confirm = pendingConfirm
            if (confirm != null) showUpdateNotification(context, confirm)
            if (AppLifecycle.updateRequested) {
                AppLifecycle.updateRequested = false
                if (confirm != null) AppLifecycle.report(context, "update", "needs_tap", "tap Update on the phone")
                else AppLifecycle.report(context, "update", "failed", "install already handed to Android; open the app on the phone")
            }
            return
        }
        val size = payload.optLong("size", 0L)
        if (size <= 0) {
            if (AppLifecycle.updateRequested) {
                AppLifecycle.updateRequested = false
                AppLifecycle.report(context, "update", "failed", "laptop reported an empty APK")
            }
            return
        }
        // A different (newer) build than we were fetching: start over.
        if (code != targetCode) {
            runCatching { raf?.close() }; raf = null
            file.delete()
            targetCode = code; downloading = false
        }
        expectedSize = size
        expectedSha = payload.optString("sha256", "")
        if (downloading) return           // a transfer is already in flight
        try {
            // Resume from whatever we already have (survives dropped connections).
            val have = if (file.exists() && file.length() in 1..expectedSize) file.length() else 0L
            raf = RandomAccessFile(file, "rw").apply { setLength(have) }
            downloading = true
            Log.d(TAG, "Updating to v${payload.optString("version_name")} — ${have}/${expectedSize} bytes")
            if (have >= expectedSize) finish() else pull(have)
        } catch (e: Exception) {
            Log.e(TAG, "Could not start update: ${e.message}")
            cleanup("could not start download: ${e.message}")
        }
    }

    /** Connection dropped mid-download: pause so the next connect resumes from the saved bytes. */
    fun onDisconnected() {
        downloading = false
        runCatching { raf?.close() }
        raf = null
    }

    private fun pull(offset: Long) {
        send(JSONObject().put("type", "APP_UPDATE_PULL").put("device_id", RelayConfig.deviceId(context))
            .put("payload", JSONObject().put("edition", edition).put("offset", offset)))
    }

    fun onChunk(header: JSONObject, payload: ByteArray) {
        val f = raf ?: return
        try {
            val offset = header.optLong("offset", 0)
            f.seek(offset)
            f.write(payload)
            val next = offset + payload.size
            if (header.optBoolean("eof", false) || next >= expectedSize) finish() else pull(next)
        } catch (e: Exception) {
            Log.e(TAG, "Chunk write failed: ${e.message}")
            cleanup("chunk write failed: ${e.message}")
        }
    }

    private fun finish() {
        try {
            raf?.close(); raf = null
            downloading = false
            if (file.length() != expectedSize) { Log.e(TAG, "Size mismatch"); cleanup("size mismatch"); return }
            if (expectedSha.isNotEmpty() && sha256(file) != expectedSha) { Log.e(TAG, "Checksum mismatch"); cleanup("checksum mismatch"); return }
            stagedCode = targetCode
            install()
        } catch (e: Exception) {
            Log.e(TAG, "Finish failed: ${e.message}")
            cleanup("install failed: ${e.message}")
        }
    }

    private fun install() {
        val installer = context.packageManager.packageInstaller
        val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL).apply {
            setAppPackageName(context.packageName)
            // Android 12+: no "Update" tap when this app is its own installer of record (or device owner).
            // If Android still wants the tap it answers PENDING_USER_ACTION and the usual flow follows.
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S)
                setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED)
        }
        val sessionId = installer.createSession(params)
        installer.openSession(sessionId).use { session ->
            session.openWrite("acc_update", 0, file.length()).use { out ->
                file.inputStream().use { it.copyTo(out) }
                session.fsync(out)
            }
            val intent = Intent(context, InstallResultReceiver::class.java)
            val flags = PendingIntent.FLAG_UPDATE_CURRENT or
                (if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) PendingIntent.FLAG_MUTABLE else 0)
            val pi = PendingIntent.getBroadcast(context, sessionId, intent, flags)
            session.commit(pi.intentSender)
        }
        Log.d(TAG, "Update staged; Android will prompt to install")
    }

    private fun cleanup(reason: String = "update failed") {
        if (AppLifecycle.updateRequested) {   // forced from the dashboard: never leave it at "accepted"
            AppLifecycle.updateRequested = false
            AppLifecycle.report(context, "update", "failed", reason)
        }
        downloading = false
        try { raf?.close() } catch (_: Exception) {}
        raf = null
        file.delete()
    }

    private fun sha256(f: File): String {
        val md = MessageDigest.getInstance("SHA-256")
        f.inputStream().use { input ->
            val buf = ByteArray(1 shl 16)
            while (true) {
                val n = input.read(buf); if (n < 0) break; md.update(buf, 0, n)
            }
        }
        return md.digest().joinToString("") { "%02x".format(it) }
    }
}

/** Android asks the user to confirm the install; this forwards that confirm screen to the foreground. */
class InstallResultReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        when (intent.getIntExtra(PackageInstaller.EXTRA_STATUS, -1)) {
            PackageInstaller.STATUS_PENDING_USER_ACTION -> {
                @Suppress("DEPRECATION")
                val confirm = intent.getParcelableExtra<Intent>(Intent.EXTRA_INTENT) ?: return
                confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                AppUpdater.pendingConfirm = confirm
                AppUpdater.showUpdateNotification(context, confirm)   // always reachable
                if (AppLifecycle.updateRequested) AppLifecycle.report(context, "update", "needs_tap", "tap Update on the phone")
                runCatching { context.startActivity(confirm) }        // opens at once when the app is in front
            }
            PackageInstaller.STATUS_SUCCESS -> {
                AppUpdater.pendingConfirm = null
                context.getSystemService(android.app.NotificationManager::class.java)?.cancel(AppUpdater.NOTIF_ID)
                Log.d("AppUpdater", "Update installed")
                if (AppLifecycle.updateRequested) { AppLifecycle.updateRequested = false; AppLifecycle.report(context, "update", "done") }
            }
            else -> {
                // cancelled or failed: offer it again on the next check (the downloaded file is reused)
                AppUpdater.pendingConfirm = null
                AppUpdater.stagedCode = 0
                if (AppLifecycle.updateRequested) {
                    AppLifecycle.updateRequested = false
                    AppLifecycle.report(context, "update", "failed", intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE) ?: "cancelled")
                }
                Log.d("AppUpdater", "Install status: ${intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE)}")
            }
        }
    }
}
