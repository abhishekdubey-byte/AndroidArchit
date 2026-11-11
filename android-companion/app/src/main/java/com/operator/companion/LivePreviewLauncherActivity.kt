package com.operator.companion

import android.app.Activity
import android.os.Bundle
import com.operator.companion.service.LivePreviewService
import com.operator.companion.service.PhoneListenService

/**
 * Momentary, invisible activity whose only job is to start the live-preview camera service from a
 * foreground state. OEM policies (ColorOS/Realme) block the camera when a service is started from
 * the background, so we flash this activity to the foreground, start the service, and finish at once.
 * The service (not this activity) holds the camera, so it keeps streaming through app-switching.
 */
class LivePreviewLauncherActivity : Activity() {
    companion object {
        const val EXTRA_TARGET = "target"
        const val TARGET_LISTEN = "listen"    // "Listen Live": a microphone service needs the same foreground start
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        overridePendingTransition(0, 0)
        // The lens was already set via LivePreviewService.setLens() before this activity was launched;
        // don't reset it here or a quick lens flip issued right after START would be lost.
        when (intent?.getStringExtra(EXTRA_TARGET)) {
            TARGET_LISTEN -> runCatching { PhoneListenService.start(applicationContext) }
            else -> runCatching { LivePreviewService.start(applicationContext) }
        }
        finish()
        overridePendingTransition(0, 0)
    }
}
