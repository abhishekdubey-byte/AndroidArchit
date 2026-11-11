package com.operator.companion.service

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class BitrateControllerTest {
    // at 2.5 Mbps: 0.3 s of backlog = 93,750 bytes, 1 s = 312,500 bytes

    @Test fun stepsDownWhenMoreThanAThirdOfASecondIsQueued() {
        val c = BitrateController(start = 2_500_000)
        assertNull(c.onQueue(80_000, nowMs = 1_000))               // 0.26 s: fine
        assertEquals(1_750_000, c.onQueue(200_000, nowMs = 2_000))  // 0.64 s
        assertEquals(1_225_000, c.onQueue(200_000, nowMs = 3_000))
    }

    @Test fun neverBelowMin() {
        val c = BitrateController(start = 1_000_000, min = 800_000)
        assertEquals(800_000, c.onQueue(900_000, nowMs = 1_000))
        assertNull(c.onQueue(900_000, nowMs = 2_000))
    }

    @Test fun firstCalmTickDoesNotStepUpImmediately() {
        val c = BitrateController(start = 1_000_000)
        assertNull(c.onQueue(0, nowMs = 1_700_000_000_000))      // real clocks are far from 0
        assertNull(c.onQueue(0, nowMs = 1_700_000_004_000))
        assertEquals(1_150_000, c.onQueue(0, nowMs = 1_700_000_005_100))
    }

    @Test fun congestionResetsTheCalmTimer() {
        val c = BitrateController(start = 1_000_000)
        c.onQueue(0, nowMs = 0)
        c.onQueue(20_000, nowMs = 3_000)                          // 0.16 s: not calm, not congested
        assertNull(c.onQueue(0, nowMs = 6_000))
    }

    @Test fun neverAboveMaxOrThermalCap() {
        val c = BitrateController(start = 3_900_000, max = 4_000_000)
        c.onQueue(0, nowMs = 0)
        assertEquals(4_000_000, c.onQueue(0, nowMs = 6_000))
        assertEquals(1_500_000, c.capAt(1_500_000))
        assertNull(c.onQueue(0, nowMs = 20_000))
        c.capAt(null)
        assertEquals(1_725_000, c.onQueue(0, nowMs = 30_000))
    }

    @Test fun skippingHasHysteresis() {
        val c = BitrateController(start = 2_500_000)
        assertFalse(c.updateSkip(200_000))      // 0.64 s queued: not yet
        assertTrue(c.updateSkip(400_000))       // 1.28 s: skip deltas
        assertTrue(c.updateSkip(100_000))       // 0.32 s: keep skipping until it really drained
        assertFalse(c.updateSkip(50_000))       // 0.16 s: resume (caller asks for one key frame)
    }
}
