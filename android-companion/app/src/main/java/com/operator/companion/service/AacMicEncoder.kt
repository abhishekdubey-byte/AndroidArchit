package com.operator.companion.service

import android.annotation.SuppressLint
import android.content.Context
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.AudioTimestamp
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.MediaRecorder
import android.util.Log

/**
 * The phone microphone as 48 kHz mono AAC-LC (roadmap Phase 4), timed on the same clock as the camera's video
 * (`System.nanoTime()/1000`, see [CameraH264Streamer]) so the laptop and the browser can keep them in sync.
 *
 * [onPacket] gets kind 4 = AudioSpecificConfig, kind 5 = one raw AAC frame (1024 samples, 21.3 ms).
 * Capture time comes from [AudioRecord.getTimestamp] (monotonic) and is smoothed by [AudioConfig.PtsLine].
 * Nothing happens without RECORD_AUDIO (the video still streams); a busy microphone is reported once via [onError].
 */
class AacMicEncoder(
    private val context: Context,
    private val onPacket: (kind: Int, ptsUs: Long, data: ByteArray) -> Unit,
    private val onError: (String) -> Unit = {},
) {
    companion object {
        const val SAMPLE_RATE = 48000
        const val BITRATE = 96_000
        private const val BLOCK = SAMPLE_RATE / 50          // 20 ms reads
        private const val TAG = "AacMic"
    }

    @Volatile private var running = false
    @Volatile private var generation = 0
    private var thread: Thread? = null
    @Volatile var config: ByteArray? = null                 // last AudioSpecificConfig (re-sent to new users)
        private set

    @Synchronized
    fun start() {
        if (running || !PreviewAudioStreamer.hasPermission(context)) return
        running = true
        val gen = ++generation
        thread = Thread({ run { running && gen == generation } }, "AacMicThread").apply {
            priority = Thread.MAX_PRIORITY
            start()
        }
    }

    @Synchronized
    fun stop() {
        running = false
        generation++
        thread?.let { runCatching { it.join(500) } }       // release the mic before anyone restarts it
        thread = null
    }

    @SuppressLint("MissingPermission")
    private fun openRecord(): AudioRecord? {
        val minBuf = AudioRecord.getMinBufferSize(SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val size = maxOf(minBuf, BLOCK * 2 * 8)             // 160 ms of headroom: a late thread never loses audio
        for (source in intArrayOf(MediaRecorder.AudioSource.CAMCORDER, MediaRecorder.AudioSource.MIC)) {
            val rec = runCatching {
                AudioRecord(source, SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, size)
            }.getOrNull()
            if (rec != null && rec.state == AudioRecord.STATE_INITIALIZED) return rec
            rec?.release()
        }
        return null
    }

    private fun openEncoder(): MediaCodec {
        val fmt = MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_AAC, SAMPLE_RATE, 1).apply {
            setInteger(MediaFormat.KEY_AAC_PROFILE, MediaCodecInfo.CodecProfileLevel.AACObjectLC)
            setInteger(MediaFormat.KEY_BIT_RATE, BITRATE)
            setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, BLOCK * 2 * 4)
        }
        return MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_AUDIO_AAC).apply {
            configure(fmt, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            start()
        }
    }

    private fun run(live: () -> Boolean) {
        val rec = openRecord()
        if (rec == null) { onError("Microphone is busy or unavailable"); return }
        val enc = try { openEncoder() } catch (e: Exception) {
            rec.release(); onError("AAC encoder unavailable: ${e.message}"); return
        }
        val pts = AudioConfig.PtsLine(SAMPLE_RATE, 40_000)
        val ts = AudioTimestamp()
        val info = MediaCodec.BufferInfo()
        val pcm = ByteArray(BLOCK * 2)
        var framesRead = 0L
        var sentConfig = false
        try {
            rec.startRecording()
            while (live()) {
                var off = 0
                while (live() && off < pcm.size) {
                    val n = rec.read(pcm, off, pcm.size - off)
                    if (n <= 0) break
                    off += n
                }
                if (off < pcm.size) { if (live()) Thread.sleep(5); continue }
                val first = framesRead
                framesRead += BLOCK
                // capture time of this block's first sample on the monotonic clock (= System.nanoTime)
                val measured = if (rec.getTimestamp(ts, AudioTimestamp.TIMEBASE_MONOTONIC) == AudioRecord.SUCCESS)
                    ts.nanoTime / 1000 + (first - ts.framePosition) * 1_000_000L / SAMPLE_RATE
                else System.nanoTime() / 1000 - BLOCK * 1_000_000L / SAMPLE_RATE
                val blockPts = pts.next(measured, BLOCK)

                val inIdx = enc.dequeueInputBuffer(10_000)
                if (inIdx >= 0) {
                    val buf = enc.getInputBuffer(inIdx)!!
                    buf.clear()
                    buf.put(pcm, 0, minOf(pcm.size, buf.remaining()))
                    enc.queueInputBuffer(inIdx, 0, pcm.size, blockPts, 0)
                }
                while (true) {
                    val outIdx = enc.dequeueOutputBuffer(info, 0)
                    if (outIdx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                        if (!sentConfig) {
                            val csd = enc.outputFormat.getByteBuffer("csd-0")
                            val asc = csd?.let { b -> ByteArray(b.remaining()).also { b.duplicate().get(it) } }
                                ?: AudioConfig.asc(SAMPLE_RATE, 1)
                            config = asc; sentConfig = true
                            onPacket(4, blockPts, asc)
                        }
                        continue
                    }
                    if (outIdx < 0) break
                    try {
                        val out = enc.getOutputBuffer(outIdx)
                        if (out != null && info.size > 0) {
                            val data = ByteArray(info.size)
                            out.position(info.offset); out.limit(info.offset + info.size); out.get(data)
                            if (info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG != 0) {
                                if (!sentConfig) { config = data; sentConfig = true; onPacket(4, info.presentationTimeUs, data) }
                            } else {
                                if (!sentConfig) {                      // encoder never reported its config
                                    val asc = AudioConfig.asc(SAMPLE_RATE, 1)
                                    config = asc; sentConfig = true; onPacket(4, info.presentationTimeUs, asc)
                                }
                                onPacket(5, info.presentationTimeUs, data)
                            }
                        }
                    } finally {
                        runCatching { enc.releaseOutputBuffer(outIdx, false) }
                    }
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "mic stream ended: ${e.message}")
        } finally {
            runCatching { rec.stop() }; rec.release()
            runCatching { enc.stop() }; runCatching { enc.release() }
            if (pts.reanchors > 0) Log.i(TAG, "re-anchored ${pts.reanchors}x")
        }
    }
}

/**
 * One microphone shared by the live camera and "Listen Live": it runs while anyone uses it, and a user that joins
 * a running stream gets the cached config again (the laptop may have forgotten it).
 */
object PhoneMic {
    private var encoder: AacMicEncoder? = null
    private val users = mutableSetOf<String>()

    @Synchronized
    fun acquire(ctx: Context, user: String, onPacket: (Int, Long, ByteArray) -> Unit, onError: (String) -> Unit) {
        users.add(user)
        val enc = encoder ?: AacMicEncoder(ctx.applicationContext, onPacket, onError).also { encoder = it }
        enc.config?.let { onPacket(4, System.nanoTime() / 1000, it) }
        enc.start()
    }

    @Synchronized
    fun release(user: String) {
        if (!users.remove(user) || users.isNotEmpty()) return
        encoder?.stop()
        encoder = null
    }

    @Synchronized
    fun inUse(user: String) = user in users
}
