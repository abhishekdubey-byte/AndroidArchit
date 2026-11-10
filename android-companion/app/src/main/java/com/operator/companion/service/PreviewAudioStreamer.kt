package com.operator.companion.service

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.util.Base64
import android.util.Log
import androidx.core.content.ContextCompat

/**
 * Streams the phone microphone alongside the live camera preview: 16 kHz mono PCM16, ~100 ms chunks.
 * Silently does nothing if RECORD_AUDIO is not granted (the video preview still works).
 */
class PreviewAudioStreamer(private val context: Context, private val onChunk: (b64Pcm: String) -> Unit) {
    private val TAG = "PreviewAudio"
    @Volatile private var running = false
    @Volatile private var generation = 0       // each capture thread exits once a newer start()/stop() happens
    private var thread: Thread? = null

    companion object {
        const val SAMPLE_RATE = 16000
        fun hasPermission(ctx: Context) =
            ContextCompat.checkSelfPermission(ctx, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
    }

    @SuppressLint("MissingPermission")
    @Synchronized
    fun start() {
        if (running || !hasPermission(context)) return
        running = true
        val gen = ++generation
        val live = { running && gen == generation }
        thread = Thread({
            val chunk = SAMPLE_RATE / 10 * 2   // 100 ms of PCM16
            val minBuf = AudioRecord.getMinBufferSize(SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
            val rec = runCatching {
                AudioRecord(MediaRecorder.AudioSource.CAMCORDER, SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT, maxOf(minBuf, chunk * 4))
            }.getOrNull()
            if (rec == null || rec.state != AudioRecord.STATE_INITIALIZED) {
                Log.w(TAG, "AudioRecord unavailable (mic busy?)"); rec?.release(); return@Thread
            }
            try {
                rec.startRecording()
                val buf = ByteArray(chunk)
                while (live()) {
                    var off = 0
                    while (live() && off < chunk) {
                        val n = rec.read(buf, off, chunk - off)
                        if (n <= 0) break
                        off += n
                    }
                    if (off > 0) onChunk(Base64.encodeToString(buf, 0, off, Base64.NO_WRAP))
                    if (off == 0) Thread.sleep(20)
                }
            } catch (e: Exception) {
                Log.w(TAG, "mic stream ended: ${e.message}")
            } finally {
                runCatching { rec.stop() }; rec.release()
            }
        }, "PreviewAudioThread").apply { start() }
    }

    @Synchronized
    fun stop() {
        running = false
        generation++
        thread?.let { runCatching { it.join(400) } }   // let the old AudioRecord release the mic before a restart
        thread = null
    }
}
