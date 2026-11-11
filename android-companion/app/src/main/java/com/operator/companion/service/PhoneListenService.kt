package com.operator.companion.service

import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.util.Log
import androidx.core.app.ServiceCompat
import com.operator.companion.network.CompanionWebSocketClient
import org.json.JSONObject

/**
 * "Listen Live": the phone microphone streamed to the dashboard as AAC (no camera), in a microphone foreground
 * service so it keeps running while the user is in other apps. Shares the mic with the live camera ([PhoneMic]).
 *
 * Platform limit (said in the dashboard): stock Android does not let apps record the other side of a phone call.
 */
class PhoneListenService : Service() {
    companion object {
        private const val TAG = "PhoneListen"
        private const val USER = "listen"
        const val ACTION_STOP = "stop"

        fun start(ctx: Context) {
            val i = Intent(ctx, PhoneListenService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) ctx.startForegroundService(i) else ctx.startService(i)
        }

        fun stop(ctx: Context) {
            runCatching { ctx.startService(Intent(ctx, PhoneListenService::class.java).setAction(ACTION_STOP)) }
                .onFailure { runCatching { ctx.stopService(Intent(ctx, PhoneListenService::class.java)) } }
        }

        fun notify(active: Boolean, error: String? = null) {
            val c = CompanionWebSocketClient.activeInstance ?: return
            val p = JSONObject().put("active", active)
            if (error != null) p.put("error", error)
            c.sendDirectJson(JSONObject().put("type", "PHONE_AUDIO_STATE").put("device_id", c.deviceId).put("payload", p))
        }
    }

    private var errorSent = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) { stopSelf(); return START_NOT_STICKY }
        if (!PreviewAudioStreamer.hasPermission(this)) {
            fail("Microphone permission is not granted on the phone"); return START_NOT_STICKY
        }
        val notif = com.operator.companion.relay.quietAppNotification(this)
        val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE else 0
        if (runCatching { ServiceCompat.startForeground(this, com.operator.companion.relay.RelayService.NOTIF_ID, notif, type) }.isFailure) {
            fail("The phone refused microphone access from the background — open the app once and try again")
            return START_NOT_STICKY
        }
        if (!PhoneMic.inUse(USER)) PhoneMic.acquire(applicationContext, USER,
            onPacket = { kind, pts, data -> CompanionWebSocketClient.activeInstance?.sendCameraPacket(kind, pts, data) },
            onError = { msg -> fail(msg) })
        notify(true)
        Log.d(TAG, "listen live started")
        return START_NOT_STICKY
    }

    private fun fail(msg: String) {
        errorSent = true
        notify(false, msg)
        stopSelf()
    }

    override fun onDestroy() {
        super.onDestroy()
        PhoneMic.release(USER)
        if (!errorSent) notify(false)
        runCatching { ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_DETACH) }
        Log.d(TAG, "listen live stopped")
    }
}
