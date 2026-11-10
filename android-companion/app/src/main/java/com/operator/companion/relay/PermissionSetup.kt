package com.operator.companion.relay

import android.Manifest
import android.app.Activity
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.os.PowerManager
import android.provider.Settings
import androidx.core.app.ActivityCompat
import com.operator.companion.BuildConfig
import com.operator.companion.service.CompanionAccessibilityService

/**
 * Everything the app needs, in the order it is requested. Android only lets the user grant these
 * (each special one on its own system screen), so the app walks through them one after another and
 * refuses to run until all are granted.
 */
object PermissionSetup {

    enum class Step(val title: String, val why: String) {
        RUNTIME("SENSES", "Eyes, ears and voice for your quests"),
        FILES("VAULT", "Reach every chest and relic on this device"),
        OVERLAY("STEALTH", "Act from the shadows, even while away"),
        ACCESSIBILITY("PILOT", "Steer the screen and command every app"),
        BATTERY("ENDLESS", "Never drop out of the game, even at rest"),
        UPDATES("EVOLVE", "Receive new powers the moment they drop"),
    }

    /** Opening the remote camera from the background needs the accessibility service or "Display over other apps".
     *  Full asks for the overlay only while accessibility is off: on ColorOS/Android 13+ sideloads its toggle is a
     *  greyed-out "restricted setting", and the overlay is then the only way. Low-RAM (Android Go) phones can't grant it. */
    private var lowRam: Boolean? = null

    fun steps(ctx: Context? = null): List<Step> = Step.values().filter { step ->
        when (step) {
            Step.ACCESSIBILITY -> !BuildConfig.LITE
            Step.OVERLAY -> !isLowRam(ctx) && (BuildConfig.LITE || ctx == null || !accessibilityEnabled(ctx))
            else -> true
        }
    }

    private fun isLowRam(ctx: Context?): Boolean {
        lowRam?.let { return it }
        val v = ctx?.let { (it.getSystemService(Context.ACTIVITY_SERVICE) as android.app.ActivityManager).isLowRamDevice } ?: return false
        lowRam = v
        return v
    }

    fun runtimePermissions(): Array<String> {
        val list = mutableListOf(
            Manifest.permission.CAMERA,
            Manifest.permission.RECORD_AUDIO,
            Manifest.permission.READ_PHONE_STATE,
            Manifest.permission.ANSWER_PHONE_CALLS,
            Manifest.permission.CALL_PHONE,
        )
        when {
            Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU -> list += listOf(
                Manifest.permission.READ_MEDIA_IMAGES,
                Manifest.permission.READ_MEDIA_VIDEO, Manifest.permission.READ_MEDIA_AUDIO)
            Build.VERSION.SDK_INT >= Build.VERSION_CODES.R -> list += Manifest.permission.READ_EXTERNAL_STORAGE
            else -> list += listOf(Manifest.permission.READ_EXTERNAL_STORAGE, Manifest.permission.WRITE_EXTERNAL_STORAGE)
        }
        return list.toTypedArray()
    }

    fun isGranted(ctx: Context, step: Step): Boolean = when (step) {
        Step.UPDATES -> ctx.packageManager.canRequestPackageInstalls()
        Step.RUNTIME -> runtimePermissions().all { ctx.checkSelfPermission(it) == PackageManager.PERMISSION_GRANTED }
        Step.FILES -> Build.VERSION.SDK_INT < Build.VERSION_CODES.R || Environment.isExternalStorageManager()
        Step.OVERLAY -> Settings.canDrawOverlays(ctx)
        Step.ACCESSIBILITY -> accessibilityEnabled(ctx)
        Step.BATTERY -> (ctx.getSystemService(Context.POWER_SERVICE) as PowerManager).isIgnoringBatteryOptimizations(ctx.packageName)
    }

    fun missing(ctx: Context): List<Step> = steps(ctx).filter { !isGranted(ctx, it) }

    fun isComplete(ctx: Context): Boolean = missing(ctx).isEmpty()

    fun accessibilityEnabled(ctx: Context): Boolean {
        if (CompanionAccessibilityService.instance != null) return true
        val enabled = Settings.Secure.getString(ctx.contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES) ?: return false
        val me = ComponentName(ctx, CompanionAccessibilityService::class.java)
        return enabled.split(':').any { ComponentName.unflattenFromString(it) == me }
    }

    /** Opens Android's screen (or dialog) for one step. */
    @android.annotation.SuppressLint("BatteryLife")
    fun launch(activity: Activity, step: Step) {
        val pkg = Uri.parse("package:${activity.packageName}")
        fun open(vararg intents: Intent) {
            for (i in intents) if (runCatching { activity.startActivity(i) }.isSuccess) return
        }
        when (step) {
            Step.UPDATES -> open(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, pkg))
            Step.RUNTIME -> {
                val prefs = activity.getSharedPreferences("setup", Context.MODE_PRIVATE)
                val missing = runtimePermissions().filter { activity.checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
                val blocked = prefs.getBoolean("asked_runtime", false) &&
                    missing.none { ActivityCompat.shouldShowRequestPermissionRationale(activity, it) }
                if (blocked) {
                    // Denied with "Don't ask again": only the app's settings page can grant them now
                    open(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, pkg))
                } else {
                    prefs.edit().putBoolean("asked_runtime", true).apply()
                    ActivityCompat.requestPermissions(activity, missing.toTypedArray(), 201)
                }
            }
            Step.FILES -> open(
                Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION, pkg),
                Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION))
            Step.OVERLAY -> open(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, pkg))
            Step.ACCESSIBILITY -> open(
                Intent("android.settings.ACCESSIBILITY_DETAILS_SETTINGS")
                    .putExtra(Intent.EXTRA_COMPONENT_NAME, ComponentName(activity, CompanionAccessibilityService::class.java).flattenToString()),
                Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
            Step.BATTERY -> open(
                Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, pkg),
                Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
        }
    }
}
