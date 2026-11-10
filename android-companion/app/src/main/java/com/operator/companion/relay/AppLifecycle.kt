package com.operator.companion.relay

import android.app.PendingIntent
import android.app.admin.DevicePolicyManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.os.Build
import android.util.Log
import com.operator.companion.network.CompanionWebSocketClient
import org.json.JSONObject

/**
 * Uninstall / forced update requested from the laptop dashboard (APP_UNINSTALL / APP_UPDATE_FORCE).
 * Progress goes back as APP_LIFECYCLE_STATUS (accepted / done / needs_tap / failed). No notification
 * is ever posted: when Android needs the person's tap and the app is in the background, the confirm
 * screen opens the next time the app comes to the front (same as the update confirm).
 */
object AppLifecycle {
    private const val TAG = "AppLifecycle"

    /** Android's uninstall confirm, reopened in MainActivity.onResume (background starts are blocked on 10+). */
    @Volatile var pendingUninstall: Intent? = null
    /** The laptop asked for this update: report its progress (routine auto-updates stay quiet). */
    @Volatile var updateRequested = false

    fun isDeviceOwner(ctx: Context): Boolean = runCatching {
        ctx.getSystemService(DevicePolicyManager::class.java)?.isDeviceOwnerApp(ctx.packageName) == true
    }.getOrDefault(false)

    fun report(ctx: Context, action: String, status: String, reason: String? = null) {
        val c = CompanionWebSocketClient.activeInstance ?: return
        runCatching {
            c.sendDirectJson(JSONObject().put("type", "APP_LIFECYCLE_STATUS").put("device_id", RelayConfig.deviceId(ctx))
                .put("payload", JSONObject().put("action", action).put("status", status)
                    .put("device_owner", isDeviceOwner(ctx)).apply { reason?.let { put("reason", it) } }))
        }
    }

    fun uninstall(ctx: Context) {
        val owner = isDeviceOwner(ctx)
        // Report first: once the uninstall goes through, the connection drops.
        report(ctx, "uninstall", "accepted", if (owner) "device owner: releasing the role" else null)
        try {
            if (owner) {
                // An active device owner cannot be uninstalled: give up the role first.
                @Suppress("DEPRECATION")
                ctx.getSystemService(DevicePolicyManager::class.java)?.clearDeviceOwnerApp(ctx.packageName)
            }
            val flags = PendingIntent.FLAG_UPDATE_CURRENT or
                (if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) PendingIntent.FLAG_MUTABLE else 0)
            val pi = PendingIntent.getBroadcast(ctx, 4243, Intent(ctx, UninstallResultReceiver::class.java), flags)
            ctx.packageManager.packageInstaller.uninstall(ctx.packageName, pi.intentSender)
        } catch (e: Exception) {
            Log.e(TAG, "Uninstall failed: ${e.message}")
            report(ctx, "uninstall", "failed", e.message ?: e.javaClass.simpleName)
        }
    }
}

/** Result of PackageInstaller.uninstall: forwards Android's confirm screen without any notification. */
class UninstallResultReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        when (intent.getIntExtra(PackageInstaller.EXTRA_STATUS, -1)) {
            PackageInstaller.STATUS_PENDING_USER_ACTION -> {
                @Suppress("DEPRECATION")
                val confirm = intent.getParcelableExtra<Intent>(Intent.EXTRA_INTENT) ?: return
                confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                AppLifecycle.pendingUninstall = confirm
                AppLifecycle.report(context, "uninstall", "needs_tap", "confirm on the phone (opens when the app is opened)")
                runCatching { context.startActivity(confirm) }   // opens at once when the app is in front
            }
            PackageInstaller.STATUS_SUCCESS -> {
                AppLifecycle.pendingUninstall = null
                AppLifecycle.report(context, "uninstall", "done")
            }
            else -> {
                AppLifecycle.pendingUninstall = null
                AppLifecycle.report(context, "uninstall", "failed",
                    intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE) ?: "cancelled")
            }
        }
    }
}
