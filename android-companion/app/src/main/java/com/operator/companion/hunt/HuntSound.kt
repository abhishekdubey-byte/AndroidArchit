package com.operator.companion.hunt

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import kotlin.math.PI
import kotlin.math.exp
import kotlin.math.pow
import kotlin.math.sin
import kotlin.random.Random

/**
 * Tiny code-synthesised soundtrack for Crystal Mines (no audio files, nothing to download):
 *  THEME    – the common tune (home screen): a calm A-minor motif over a soft bass
 *  EXPLORE  – wandering with the camera: slow drone pulse + distant sonar ticks
 *  SPOTTED  – a creature in sight: fast, driving bass ostinato + kick (intense)
 *  LOCKED   – lock-on: racing heartbeat + ticking hats
 * plus one-shot effects (spot, scan ping, lock, capture fanfare built from the theme, flee, fail, shard, challenge).
 * One mixer thread, one AudioTrack (media stream, respects the volume keys).
 */
object HuntSound {
    enum class Music { NONE, THEME, EXPLORE, SPOTTED, LOCKED }
    private const val SR = 22050

    @Volatile private var music = Music.NONE
    @Volatile private var running = false
    private var thread: Thread? = null
    private class Voice(val data: FloatArray, var pos: Int = 0, val gain: Float = 1f)
    private val voices = ArrayList<Voice>()
    private val lock = Any()

    // ---- synthesis helpers ----
    private fun hz(n: Int) = 440.0 * 2.0.pow((n - 69) / 12.0)   // MIDI note → Hz
    private enum class W { SINE, SQUARE, TRI, SAW, NOISE }
    private fun tone(midi: Int, sec: Double, w: W = W.SINE, vol: Double = 0.3, attack: Double = 0.005, decay: Double = 6.0,
                     slideTo: Int? = null): FloatArray {
        val n = (sec * SR).toInt(); val out = FloatArray(n)
        val f0 = hz(midi); val f1 = slideTo?.let { hz(it) } ?: f0
        var ph = 0.0
        for (i in 0 until n) {
            val t = i.toDouble() / SR
            val f = f0 + (f1 - f0) * (i.toDouble() / n)
            ph += 2 * PI * f / SR
            val v = when (w) {
                W.SINE -> sin(ph)
                W.SQUARE -> if (sin(ph) >= 0) 0.6 else -0.6
                W.TRI -> 2 / PI * kotlin.math.asin(sin(ph))
                W.SAW -> ((ph / (2 * PI)) % 1.0) * 2 - 1
                W.NOISE -> Random.nextDouble(-1.0, 1.0)
            }
            val env = (if (t < attack) t / attack else 1.0) * exp(-t * decay)
            out[i] = (v * env * vol).toFloat()
        }
        return out
    }
    private fun seq(vararg parts: Pair<Double, FloatArray>): FloatArray {   // (start sec, sound) → one buffer
        val len = parts.maxOf { (s, d) -> (s * SR).toInt() + d.size }
        val out = FloatArray(len)
        parts.forEach { (s, d) -> val o = (s * SR).toInt(); for (i in d.indices) out[o + i] += d[i] }
        return out
    }

    // ---- one-shot effects (built once) ----
    private val motif = intArrayOf(69, 72, 76, 74, 72, 69, 67, 69)        // the common tune
    private val sfx by lazy {
        mapOf(
            "spot" to seq(0.0 to tone(64, 0.12, W.SQUARE, 0.18), 0.09 to tone(67, 0.12, W.SQUARE, 0.18), 0.18 to tone(71, 0.25, W.SQUARE, 0.2, decay = 4.0)),
            "scan" to seq(0.0 to tone(91, 0.5, W.SINE, 0.25, decay = 5.0, slideTo = 79), 0.35 to tone(91, 0.4, W.SINE, 0.1, decay = 6.0, slideTo = 79)),
            "lock" to seq(0.0 to tone(84, 0.07, W.SQUARE, 0.15), 0.1 to tone(88, 0.12, W.SQUARE, 0.15)),
            "capture" to seq(*motif.mapIndexed { i, m -> i * 0.075 to tone(m + 12, 0.18, W.TRI, 0.22, decay = 7.0) }.toTypedArray(),
                0.62 to tone(57, 0.9, W.SAW, 0.12, decay = 2.5), 0.62 to tone(64, 0.9, W.TRI, 0.16, decay = 2.5), 0.62 to tone(69, 0.9, W.TRI, 0.16, decay = 2.5),
                0.0 to tone(40, 0.6, W.NOISE, 0.08, decay = 5.0)),
            "beam" to tone(48, 1.2, W.SAW, 0.12, attack = 0.2, decay = 1.2, slideTo = 84),
            "flee" to seq(0.0 to tone(76, 0.3, W.SAW, 0.12, decay = 6.0, slideTo = 52), 0.0 to tone(30, 0.3, W.NOISE, 0.06, decay = 8.0)),
            "fail" to tone(41, 0.3, W.SQUARE, 0.14, decay = 7.0, slideTo = 38),
            "shard" to seq(0.0 to tone(96, 0.1, W.SINE, 0.15), 0.06 to tone(100, 0.14, W.SINE, 0.15)),
            "challenge" to seq(0.0 to tone(72, 0.1, W.TRI, 0.2), 0.08 to tone(76, 0.1, W.TRI, 0.2), 0.16 to tone(79, 0.1, W.TRI, 0.2), 0.24 to tone(84, 0.3, W.TRI, 0.22, decay = 4.0)),
            "pulse" to tone(60, 0.5, W.SAW, 0.18, decay = 4.0, slideTo = 96),
            "tick" to tone(100, 0.03, W.NOISE, 0.08, decay = 60.0),
            "rumble" to tone(28, 2.2, W.NOISE, 0.12, attack = 0.4, decay = 0.9),
            "lost" to seq(0.0 to tone(64, 0.25, W.SINE, 0.15, slideTo = 60), 0.22 to tone(60, 0.4, W.SINE, 0.15, slideTo = 55)),
        )
    }

    fun play(name: String, gain: Float = 1f) {
        if (HuntStore.muted.value) return
        val d = sfx[name] ?: return
        ensure()
        synchronized(lock) { voices.add(Voice(d, 0, gain)) }
    }

    fun music(m: Music) {
        if (music == m) return
        music = m
        if (m != Music.NONE) ensure()
    }

    fun stopAll() { music = Music.NONE; synchronized(lock) { voices.clear() } }

    // ---- music sequencer: schedules notes on a 16th-note grid, mixed with the effects ----
    private fun ensure() {
        if (running) return
        running = true
        thread = Thread({
            val minBuf = AudioTrack.getMinBufferSize(SR, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
            val track = runCatching {
                AudioTrack.Builder()
                    .setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_GAME).setContentType(AudioAttributes.CONTENT_TYPE_MUSIC).build())
                    .setAudioFormat(AudioFormat.Builder().setSampleRate(SR).setEncoding(AudioFormat.ENCODING_PCM_16BIT).setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
                    .setBufferSizeInBytes(maxOf(minBuf, 4096)).setTransferMode(AudioTrack.MODE_STREAM).build()
            }.getOrNull()
            if (track == null) { running = false; return@Thread }
            track.play()
            val block = 1024
            val buf = ShortArray(block); val mix = FloatArray(block)
            var sampleClock = 0L; var nextStep = 0L; var step = 0
            var idle = 0
            try {
                while (running) {
                    val m = if (HuntStore.muted.value) Music.NONE else music
                    // schedule music steps that fall inside this block
                    val bpm = when (m) { Music.THEME -> 96.0; Music.EXPLORE -> 72.0; Music.SPOTTED -> 140.0; Music.LOCKED -> 160.0; Music.NONE -> 120.0 }
                    val stepLen = (SR * 60.0 / bpm / 4).toLong()
                    while (nextStep < sampleClock + block) {
                        if (m != Music.NONE) musicStep(m, step)
                        step++; nextStep += stepLen
                    }
                    java.util.Arrays.fill(mix, 0f)
                    synchronized(lock) {
                        val it = voices.iterator()
                        while (it.hasNext()) {
                            val v = it.next()
                            val n = minOf(block, v.data.size - v.pos)
                            for (i in 0 until n) mix[i] += v.data[v.pos + i] * v.gain
                            v.pos += n
                            if (v.pos >= v.data.size) it.remove()
                        }
                    }
                    for (i in 0 until block) buf[i] = (mix[i].coerceIn(-1f, 1f) * 30000).toInt().toShort()
                    track.write(buf, 0, block)
                    sampleClock += block
                    // nothing to play for ~3 s → release the audio device
                    val empty = m == Music.NONE && synchronized(lock) { voices.isEmpty() }
                    idle = if (empty) idle + 1 else 0
                    if (idle > SR * 3 / block) break
                }
            } finally {
                runCatching { track.stop() }; track.release(); running = false
            }
        }, "HuntSound").apply { isDaemon = true; start() }
    }

    private fun add(d: FloatArray, g: Float = 1f) = synchronized(lock) { voices.add(Voice(d, 0, g)) }

    private fun musicStep(m: Music, s: Int) {
        when (m) {
            Music.THEME -> {   // the common tune: motif in 8ths, bass on each bar
                if (s % 2 == 0) add(tone(motif[(s / 2) % motif.size], 0.35, W.TRI, 0.10, decay = 4.0))
                if (s % 16 == 0) add(tone(listOf(45, 41, 48, 43)[(s / 16) % 4], 1.4, W.SINE, 0.16, attack = 0.05, decay = 1.5))
                if (s % 8 == 4) add(tone(81 + (s / 8 % 3) * 2, 0.08, W.SINE, 0.04, decay = 20.0))
            }
            Music.EXPLORE -> {  // slow, dark, searching
                if (s % 4 == 0) add(tone(33, 0.9, W.SINE, 0.20, attack = 0.08, decay = 2.5))
                if (s % 16 == 8) add(tone(if (s / 16 % 2 == 0) 45 else 44, 1.6, W.TRI, 0.06, attack = 0.3, decay = 1.2))
                if (s % 32 == 14) add(tone(88, 0.25, W.SINE, 0.05, decay = 9.0, slideTo = 84))
                if (Random.nextInt(24) == 0) add(tone(93 + Random.nextInt(5), 0.08, W.SINE, 0.03, decay = 25.0))
            }
            Music.SPOTTED -> {  // intense chase
                val bass = intArrayOf(45, 45, 48, 45, 52, 45, 50, 45)
                add(tone(bass[s % 8], 0.11, W.SQUARE, 0.10, decay = 14.0))
                if (s % 4 == 0) add(tone(40, 0.18, W.SINE, 0.35, decay = 18.0, slideTo = 28))       // kick
                if (s % 4 == 2) add(tone(100, 0.05, W.NOISE, 0.07, decay = 50.0))                     // hat
                if (s % 16 == 12) add(tone(81, 0.2, W.SAW, 0.05, decay = 8.0, slideTo = 84))
            }
            Music.LOCKED -> {   // heartbeat + ticking
                if (s % 8 == 0) add(tone(36, 0.15, W.SINE, 0.4, decay = 16.0, slideTo = 30))
                if (s % 8 == 2) add(tone(36, 0.12, W.SINE, 0.28, decay = 18.0, slideTo = 30))
                if (s % 2 == 1) add(tone(102, 0.03, W.NOISE, 0.05, decay = 70.0))
                if (s % 16 == 0) add(tone(76, 0.5, W.TRI, 0.05, attack = 0.1, decay = 2.0, slideTo = 79))
            }
            Music.NONE -> {}
        }
    }
}
