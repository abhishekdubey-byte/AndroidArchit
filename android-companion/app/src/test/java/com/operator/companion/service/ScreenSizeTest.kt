package com.operator.companion.service

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ScreenSizeTest {
    @Test fun tallPhoneScaledToLongSide() {
        assertEquals(640 to 1280, ScreenSize.choose(1080, 2160))
        assertEquals(1280 to 640, ScreenSize.choose(2160, 1080))     // landscape
    }

    @Test fun oddAspectGivesEvenSides() {
        val (w, h) = ScreenSize.choose(1080, 2340)                    // 19.5:9
        assertEquals(1280, h)
        assertEquals(0, w % 2)
        assertTrue(kotlin.math.abs(w.toDouble() / h - 1080.0 / 2340) < 0.01)
    }

    @Test fun smallDisplayNotUpscaledButEven() {
        assertEquals(480 to 854, ScreenSize.choose(480, 854))
        assertEquals(480 to 852, ScreenSize.choose(481, 853))
    }

    @Test fun customLongSide() {
        assertEquals(360 to 720, ScreenSize.choose(1080, 2160, 720))
    }

    @Test fun bitrateStaysInRange() {
        assertEquals(2_457_600, ScreenSize.bitrate(640, 1280))
        assertEquals(1_500_000, ScreenSize.bitrate(360, 720))
        assertEquals(3_000_000, ScreenSize.bitrate(1080, 2160))
    }
}
