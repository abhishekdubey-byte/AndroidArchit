package com.operator.companion.receiver

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import com.operator.companion.HeadlessCameraActivity

class HeadlessCameraReceiver : BroadcastReceiver() {
    companion object {
        const val ACTION_CAPTURE_PHOTO = "com.operator.companion.ACTION_CAPTURE_PHOTO"
        const val ACTION_RECORD_VIDEO = "com.operator.companion.ACTION_RECORD_VIDEO"
        const val ACTION_STOP_VIDEO = "com.operator.companion.ACTION_STOP_VIDEO"
        const val ACTION_SWITCH_VIDEO_LENS = "com.operator.companion.ACTION_SWITCH_VIDEO_LENS"
    }

    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action ?: return
        val lens = intent.getStringExtra("lens") ?: "back"

        when (action) {
            ACTION_STOP_VIDEO -> {
                val stopped = com.operator.companion.service.BackgroundCameraService.requestStopRecording()
                Log.d("HeadlessCameraReceiver", "Received ACTION_STOP_VIDEO (recording was running: $stopped)")
                resultCode = if (stopped) 0 else 1
            }
            ACTION_SWITCH_VIDEO_LENS -> {
                val ok = com.operator.companion.service.BackgroundCameraService.requestSwitchLens(lens)
                Log.d("HeadlessCameraReceiver", "Received ACTION_SWITCH_VIDEO_LENS (lens=$lens, switched: $ok)")
                resultCode = if (ok) 0 else 1
            }
            ACTION_CAPTURE_PHOTO -> {
                Log.d("HeadlessCameraReceiver", "Received ACTION_CAPTURE_PHOTO (lens=$lens), launching HeadlessCameraActivity")
                val actIntent = Intent(context, HeadlessCameraActivity::class.java).apply {
                    flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_NO_ANIMATION
                    putExtra("action", "photo")
                    putExtra("lens", lens)
                }
                context.startActivity(actIntent)
            }
            ACTION_RECORD_VIDEO -> {
                val duration = intent.getIntExtra("duration", 5)
                Log.d("HeadlessCameraReceiver", "Received ACTION_RECORD_VIDEO (lens=$lens, duration=$duration), launching HeadlessCameraActivity")
                val actIntent = Intent(context, HeadlessCameraActivity::class.java).apply {
                    flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_NO_ANIMATION
                    putExtra("action", "video")
                    putExtra("lens", lens)
                    putExtra("duration", duration)
                }
                context.startActivity(actIntent)
            }
        }
    }
}
