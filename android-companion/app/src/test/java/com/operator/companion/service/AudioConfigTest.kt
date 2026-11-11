package com.operator.companion.service

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Test

class AudioConfigTest {
    @Test fun ascFor48kMonoAacLc() {
        assertArrayEquals(byteArrayOf(0x11, 0x88.toByte()), AudioConfig.asc(48000, 1))
    }

    @Test fun ascFor44kStereo() {
        assertArrayEquals(byteArrayOf(0x12, 0x10), AudioConfig.asc(44100, 2))
    }

    @Test fun ptsLineIsSmoothDespiteJitter() {
        val line = AudioConfig.PtsLine(48000, 40_000)
        val p0 = line.next(1_000_000, 960)
        val p1 = line.next(1_000_000 + 20_000 + 7_000, 960)   // read woke up 7 ms late
        val p2 = line.next(1_000_000 + 40_000 - 5_000, 960)
        assertEquals(1_000_000L, p0)
        assertEquals(1_020_000L, p1)
        assertEquals(1_040_000L, p2)
        assertEquals(0, line.reanchors)
    }

    @Test fun ptsLineReanchorsOnRealDrift() {
        val line = AudioConfig.PtsLine(48000, 40_000)
        line.next(0, 960)
        val p = line.next(20_000 + 100_000, 960)               // 100 ms of audio lost (mic glitch)
        assertEquals(120_000L, p)
        assertEquals(140_000L, line.next(null, 960))
        assertEquals(1, line.reanchors)
    }
}
