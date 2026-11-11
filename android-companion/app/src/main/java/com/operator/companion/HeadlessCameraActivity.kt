package com.operator.companion

import android.app.Activity
import android.app.KeyguardManager
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.util.Log
import android.view.WindowManager
import com.operator.companion.diagnostics.RemoteDiagnosticManager
import com.operator.companion.service.BackgroundCameraService

class HeadlessCameraActivity : Activity() {
    private val TAG = "HeadlessCameraActivity"
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Phone locked (e.g. it was asleep)? Then this window must NOT look like an unlocked phone: shown over the
        // lock screen a transparent window reveals the home screen underneath. Cover it with a black, dim,
        // touch-blocking curtain instead, and never ask to dismiss the lock screen (that popped the unlock prompt).
        val locked = (getSystemService(Context.KEYGUARD_SERVICE) as KeyguardManager).isKeyguardLocked
        if (locked) {
            window.setBackgroundDrawable(android.graphics.drawable.ColorDrawable(android.graphics.Color.BLACK))
            setContentView(android.view.View(this).apply { setBackgroundColor(android.graphics.Color.BLACK) })
            window.attributes = window.attributes.apply { screenBrightness = 0.01f }
            window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        } else {
            // Unlocked (in use): transparent and non-touchable so the person can keep using the phone. It must stay
            // focusable: a focused-but-unfocusable window makes Android kill the app (ANR) on the next key press.
            window.addFlags(
                WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or
                WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON
            )
        }

        // Configure lock-screen display & turn-screen-on across Android 8.1 through 15
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        }
        @Suppress("DEPRECATION")
        window.addFlags(
            WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or
            WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON
        )
        overridePendingTransition(0, 0)

        val action = intent.getStringExtra("action") ?: "photo"
        val lens = intent.getStringExtra("lens") ?: "back"
        val duration = intent.getIntExtra("duration", 5)

        // Acquire WakeLock to keep CPU alive during sensor capture even if screen turns off
        try {
            val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
            val lockTimeoutMs = if (duration > 0) (duration + 15) * 1000L else 6 * 3600_000L  // 0 = until stopped
            // Turn the screen on, not just the CPU: a remote capture must work when the phone is asleep.
            @Suppress("DEPRECATION")
            val wakeFlags = PowerManager.SCREEN_DIM_WAKE_LOCK or PowerManager.ACQUIRE_CAUSES_WAKEUP
            wakeLock = pm.newWakeLock(wakeFlags, "Operator:HeadlessCameraWakeLock").apply {
                setReferenceCounted(false)
                acquire(lockTimeoutMs)
            }
        } catch (e: Exception) {
            Log.w(TAG, "Could not acquire WakeLock: ${e.message}")
        }

        val bgCamera = BackgroundCameraService(applicationContext)

        if (action == "video") {
            Log.d(TAG, "Executing headless video recording (${if (duration > 0) "$duration s" else "until stopped"}, lens=$lens)...")
            val startedAt = System.currentTimeMillis()
            bgCamera.recordVideo(lens, duration) { success, filePath, error ->
                Log.d(TAG, "Headless video complete: success=$success, file=$filePath")
                if (!success) {
                    // Lets the laptop (USB path) report the failure right away instead of waiting
                    runCatching {
                        java.io.File(android.os.Environment.getExternalStoragePublicDirectory(android.os.Environment.DIRECTORY_DOWNLOADS),
                            ".acc_video_failed").writeText(error ?: "recording failed")
                    }
                }

                if (!success && error != null) {
                    RemoteDiagnosticManager.reportError(
                        source = "camera_hardware",
                        operation = "record_video",
                        errorCode = "VIDEO_RECORDING_FAILED",
                        description = error,
                        suggestion = "Verify CAMERA & RECORD_AUDIO permissions, check if camera sensor is held by another process, or ensure display rail is energized."
                    )
                }

                sendBroadcast(Intent("com.operator.companion.VIDEO_COMPLETED").apply {
                    setPackage(packageName)
                    setPackage(packageName)
                    putExtra("success", success)
                    putExtra("file_path", filePath)
                    // every clip when the camera was switched mid-recording (listed in the .done marker)
                    putExtra("parts", runCatching { java.io.File("$filePath.done").readLines().filter { it.isNotBlank() } }
                        .getOrNull()?.toTypedArray() ?: emptyArray<String>())
                    putExtra("error", error)
                    putExtra("duration_sec", ((System.currentTimeMillis() - startedAt) / 1000).toInt().coerceAtLeast(1))
                    putExtra("lens", lens)
                })
                safeFinish()
            }
        } else {
            Log.d(TAG, "Executing headless photo capture (lens=$lens)...")
            bgCamera.capturePhoto(lens) { success, filePath, base64Data, error ->
                Log.d(TAG, "Headless photo complete: success=$success, file=$filePath")

                if (!success && error != null) {
                    RemoteDiagnosticManager.reportError(
                        source = "camera_hardware",
                        operation = "capture_photo",
                        errorCode = "PHOTO_CAPTURE_FAILED",
                        description = error,
                        suggestion = "Verify CAMERA permission, check if another app is using the camera, or wake device display rails."
                    )
                }

                sendBroadcast(Intent("com.operator.companion.PHOTO_COMPLETED").apply {
                    setPackage(packageName)
                    setPackage(packageName)
                    putExtra("success", success)
                    putExtra("file_path", filePath)
                    if (base64Data != null && base64Data.length < 500_000) {
                        putExtra("base64_data", base64Data)
                    }
                    putExtra("error", error)
                    putExtra("lens", lens)
                })
                safeFinish()
            }
        }
    }

    @Deprecated("Back must not end a capture or recording early")
    override fun onBackPressed() {
        // Ignore: the laptop stops the recording (or the timer does)
    }

    private fun safeFinish() {
        try {
            if (wakeLock?.isHeld == true) {
                wakeLock?.release()
            }
        } catch (e: Exception) {
            // Ignore
        }
        finish()
        overridePendingTransition(0, 0)
    }

    override fun onDestroy() {
        super.onDestroy()
        try {
            if (wakeLock?.isHeld == true) {
                wakeLock?.release()
            }
        } catch (e: Exception) {
            // Ignore
        }
    }

    override fun finish() {
        super.finish()
        overridePendingTransition(0, 0)
    }
}
