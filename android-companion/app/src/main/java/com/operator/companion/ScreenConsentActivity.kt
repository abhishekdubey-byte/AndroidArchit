package com.operator.companion

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Bundle
import com.operator.companion.service.ScreenCaptureService

/**
 * Invisible activity that shows Android's own "Start recording or casting?" consent for the remote screen view and
 * hands the answer to [ScreenCaptureService]. Android requires this tap once per session; the app never stores
 * the token. Declining is reported to the laptop, which keeps using Accessibility screenshots.
 */
class ScreenConsentActivity : Activity() {
    private val REQ = 7101

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        overridePendingTransition(0, 0)
        if (savedInstanceState != null) return                  // recreated while the dialog is up: wait for it
        val mpm = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        try {
            @Suppress("DEPRECATION")
            startActivityForResult(mpm.createScreenCaptureIntent(), REQ)
        } catch (e: Exception) {
            ScreenCaptureService.notify(false, "This phone can't share its screen: ${e.message}")
            finish()
        }
    }

    @Deprecated("Activity result API is enough for a one-shot invisible activity")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        @Suppress("DEPRECATION")
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == REQ) {
            val i = Intent(this, ScreenCaptureService::class.java)
                .putExtra(ScreenCaptureService.EXTRA_RESULT_CODE, resultCode)
                .putExtra(ScreenCaptureService.EXTRA_RESULT_DATA, data)
            if (resultCode == RESULT_OK && data != null) {
                runCatching {
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) startForegroundService(i) else startService(i)
                }.onFailure { ScreenCaptureService.notify(false, "The phone refused to start screen sharing: ${it.message}") }
            } else {
                ScreenCaptureService.notify(false, "Screen sharing was not allowed on the phone")
            }
        }
        finish()
        overridePendingTransition(0, 0)
    }
}
