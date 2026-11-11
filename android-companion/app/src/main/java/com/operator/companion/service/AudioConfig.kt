package com.operator.companion.service

/** AAC-LC helpers shared by the live microphone stream (no Android APIs: unit-tested on the JVM). */
object AudioConfig {
    private val RATES = intArrayOf(96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350)

    fun rateIndex(sampleRate: Int): Int = RATES.indexOf(sampleRate).also {
        require(it >= 0) { "unsupported AAC sample rate $sampleRate" }
    }

    /** MPEG-4 AudioSpecificConfig for AAC-LC (object type 2): 5 bits type, 4 bits rate index, 4 bits channels. */
    fun asc(sampleRate: Int, channels: Int): ByteArray {
        val idx = rateIndex(sampleRate)
        val bits = (2 shl 11) or (idx shl 7) or (channels shl 3)
        return byteArrayOf((bits shr 8).toByte(), (bits and 0xFF).toByte())
    }

    /**
     * Presentation time (µs) of the next 20 ms block, kept on one smooth sample-counted line and re-anchored only
     * when the measured capture time drifts away from it (so the stream never jitters but cannot drift either).
     */
    class PtsLine(private val sampleRate: Int, private val maxDriftUs: Long) {
        private var anchorUs = Long.MIN_VALUE
        private var samples = 0L
        var reanchors = 0
            private set

        /** [measuredUs] = capture time of the block's first sample, or null when unknown. */
        fun next(measuredUs: Long?, blockSamples: Int): Long {
            var pts = if (anchorUs == Long.MIN_VALUE) (measuredUs ?: 0L) else anchorUs + samples * 1_000_000L / sampleRate
            if (anchorUs == Long.MIN_VALUE) {
                anchorUs = pts
            } else if (measuredUs != null && kotlin.math.abs(measuredUs - pts) > maxDriftUs) {
                anchorUs = measuredUs - samples * 1_000_000L / sampleRate
                pts = measuredUs
                reanchors++
            }
            samples += blockSamples
            return pts
        }
    }
}
