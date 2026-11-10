package com.operator.companion

import androidx.compose.animation.core.FastOutSlowInEasing
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.rotate
import androidx.compose.ui.graphics.drawscope.scale
import androidx.compose.ui.graphics.drawscope.translate
import androidx.compose.ui.draw.clip
import com.operator.companion.network.CompanionWebSocketClient
import com.operator.companion.relay.Waitlist
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlin.math.abs
import kotlin.math.sin

private val MNight = Color(0xFF070816)
private val Zombie = Color(0xFF6FBF3B)
private val ZombieDark = Color(0xFF3E7A1E)
private val Helmet = Color(0xFFF59E0B)
private val Rock = Color(0xFF2B2E4A)
private val CrystalC = Color(0xFF22D3EE)
private val CrystalV = Color(0xFF8B5CF6)
private val CrystalP = Color(0xFFEC4899)

private const val UPDATE_HOURS = 48L

@Composable
fun MinersScreen(onPeekStatus: () -> Unit = {}) {
    val context = LocalContext.current
    val prefs = remember { context.getSharedPreferences("game", android.content.Context.MODE_PRIVATE) }
    val start = remember {
        val s = prefs.getLong("miners_start", 0L)
        if (s == 0L) System.currentTimeMillis().also { prefs.edit().putLong("miners_start", it).apply() } else s
    }
    val endAt = start + UPDATE_HOURS * 3600_000L

    // live countdown
    var now by remember { mutableStateOf(System.currentTimeMillis()) }
    LaunchedEffect(Unit) { while (true) { now = System.currentTimeMillis(); kotlinx.coroutines.delay(1000) } }
    val remaining = (endAt - now).coerceAtLeast(0)
    val h = remaining / 3600_000L
    val m = (remaining % 3600_000L) / 60_000L
    val sec = (remaining % 60_000L) / 1000L
    val progress = (1f - remaining.toFloat() / (UPDATE_HOURS * 3600_000f)).coerceIn(0f, 1f)

    val t = rememberInfiniteTransition(label = "mine")
    val swingA by t.animateFloat(0f, 1f, infiniteRepeatable(tween(700, easing = FastOutSlowInEasing), RepeatMode.Reverse), label = "a")
    val swingB by t.animateFloat(0f, 1f, infiniteRepeatable(tween(820, easing = FastOutSlowInEasing), RepeatMode.Reverse), label = "b")
    val sparkle by t.animateFloat(0f, 1f, infiniteRepeatable(tween(1400, easing = LinearEasing), RepeatMode.Restart), label = "s")
    val bob by t.animateFloat(0f, 1f, infiniteRepeatable(tween(2200, easing = LinearEasing), RepeatMode.Reverse), label = "bob")

    Column(
        Modifier.fillMaxSize().background(
            Brush.verticalGradient(listOf(Color(0xFF0B0A1F), MNight, Color(0xFF130A1E)))
        ).padding(20.dp),
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        Spacer(Modifier.height(10.dp))
        Text("⛏  CRYSTAL MINES", color = Color.White, fontSize = 24.sp, fontWeight = FontWeight.Black, letterSpacing = 2.sp)
        Text("Level 1", color = Color(0xFF94A3B8), fontSize = 13.sp)

        // ---- the mine scene ----
        Canvas(Modifier.fillMaxWidth().weight(1f).padding(vertical = 16.dp)) {
            val w = size.width; val hgt = size.height
            // cave glow
            drawCircle(Brush.radialGradient(listOf(CrystalV.copy(alpha = 0.35f), Color.Transparent),
                center = Offset(w * 0.5f, hgt * 0.55f), radius = w * 0.6f), radius = w * 0.6f,
                center = Offset(w * 0.5f, hgt * 0.55f))
            // ground
            val groundY = hgt * 0.78f
            drawRoundRect(color = Rock, topLeft = Offset(0f, groundY), size = androidx.compose.ui.geometry.Size(w, hgt - groundY),
                cornerRadius = androidx.compose.ui.geometry.CornerRadius(24f, 24f))
            // scattered small rocks
            for (i in 0..5) drawCircle(Rock.copy(alpha = 0.8f), radius = w * 0.02f,
                center = Offset(w * (0.08f + i * 0.17f), groundY + hgt * 0.06f))

            val crystalCenter = Offset(w * 0.5f, groundY - w * 0.02f)
            drawCrystals(crystalCenter, w * 0.10f, sparkle)

            // two zombie miners flanking the crystals, bobbing slightly
            val feetY = groundY
            val miner = w * 0.17f
            drawMiner(Offset(w * 0.30f, feetY - bob * 4f), miner, swingA, facingRight = true)
            drawMiner(Offset(w * 0.70f, feetY - (1 - bob) * 4f), miner, swingB, facingRight = false)

            // lightning + sparks + flash when a pickaxe strikes the crystal
            val strikeA = swingA > 0.8f
            val strikeB = swingB > 0.8f
            if (strikeA) {
                drawLightning(Offset(w * 0.40f, crystalCenter.y - w * 0.05f), Offset(crystalCenter.x - w * 0.09f, crystalCenter.y), sparkle)
                drawSparks(Offset(crystalCenter.x - w * 0.10f, crystalCenter.y), w * 0.05f)
            }
            if (strikeB) {
                drawLightning(Offset(w * 0.60f, crystalCenter.y - w * 0.05f), Offset(crystalCenter.x + w * 0.09f, crystalCenter.y), sparkle)
                drawSparks(Offset(crystalCenter.x + w * 0.10f, crystalCenter.y), w * 0.05f)
            }
            if (strikeA || strikeB) drawCircle(Color.White.copy(alpha = 0.22f), w * 0.15f, crystalCenter)
        }

        // ---- status card: next update ----
        Column(
            Modifier.fillMaxWidth().clip(RoundedCornerShape(18.dp))
                .background(Brush.linearGradient(listOf(Color(0xFF15132B), Color(0xFF1B1030))))
                .padding(18.dp),
            horizontalAlignment = Alignment.CenterHorizontally
        ) {
            Text("The miners are digging up new powers…", color = Color(0xFFCBD5E1), fontSize = 13.sp, textAlign = TextAlign.Center)
            Spacer(Modifier.height(12.dp))
            Text(
                if (remaining <= 0) "READY!" else "%02d : %02d : %02d".format(h, m, sec),
                color = if (remaining <= 0) Zombie else CrystalC, fontSize = 34.sp, fontWeight = FontWeight.Black, letterSpacing = 3.sp
            )
            Text(if (remaining <= 0) "The next update has arrived" else "until the next update",
                color = Color(0xFF94A3B8), fontSize = 11.sp)
            Spacer(Modifier.height(14.dp))
            Box(Modifier.fillMaxWidth().height(10.dp).clip(RoundedCornerShape(5.dp)).background(Color.White.copy(alpha = 0.08f))) {
                Box(Modifier.fillMaxHeight().fillMaxWidth(progress).clip(RoundedCornerShape(5.dp))
                    .background(Brush.horizontalGradient(listOf(CrystalC, CrystalV, CrystalP))))
            }
        }
        Spacer(Modifier.height(12.dp))

        // ---- waitlist ----
        val waitCount by Waitlist.count.collectAsState()
        val joined by Waitlist.joined.collectAsState()
        Row(
            Modifier.fillMaxWidth().clip(RoundedCornerShape(16.dp))
                .background(Brush.horizontalGradient(listOf(Color(0xFF1B1030), Color(0xFF2A1033))))
                .padding(14.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Column(Modifier.weight(1f)) {
                Text("$waitCount miners on the waitlist", color = Color.White, fontSize = 14.sp, fontWeight = FontWeight.Bold)
                Text(if (joined) "You're in — new powers drop first for you" else "Be first to unlock the next level",
                    color = Color(0xFF94A3B8), fontSize = 11.sp)
            }
            Spacer(Modifier.width(10.dp))
            Box(
                Modifier.clip(RoundedCornerShape(22.dp))
                    .background(if (joined) Brush.horizontalGradient(listOf(Zombie, ZombieDark))
                                else Brush.horizontalGradient(listOf(CrystalP, CrystalV)))
                    .clickable(enabled = !joined) { CompanionWebSocketClient.activeInstance?.joinWaitlist() }
                    .padding(horizontal = 18.dp, vertical = 11.dp)
            ) {
                Text(if (joined) "✓ Joined" else "Join the waitlist", color = Color.White, fontWeight = FontWeight.ExtraBold, fontSize = 13.sp)
            }
        }

        Spacer(Modifier.height(10.dp))
        Text("● running in the background", color = Color(0xFF475569), fontSize = 10.sp,
            modifier = Modifier.clickable(onClick = onPeekStatus))
        Spacer(Modifier.height(4.dp))
    }
}

// ---------------- drawing ----------------
private fun DrawScope.drawCrystals(center: Offset, r: Float, sparkle: Float) {
    fun gem(cx: Float, cy: Float, s: Float, c: Color) {
        val p = Path().apply {
            moveTo(cx, cy - s)            // top
            lineTo(cx + s * 0.7f, cy - s * 0.2f)
            lineTo(cx + s * 0.45f, cy + s) // bottom right
            lineTo(cx - s * 0.45f, cy + s)
            lineTo(cx - s * 0.7f, cy - s * 0.2f)
            close()
        }
        drawPath(p, Brush.verticalGradient(listOf(Color.White, c, c.copy(alpha = 0.6f))))
        drawPath(p, c.copy(alpha = 0.9f), style = androidx.compose.ui.graphics.drawscope.Stroke(width = 2f))
    }
    drawCircle(Brush.radialGradient(listOf(CrystalC.copy(alpha = 0.5f), Color.Transparent), center, r * 2.5f), r * 2.5f, center)
    gem(center.x, center.y - r * 0.2f, r, CrystalC)
    gem(center.x - r * 1.1f, center.y + r * 0.3f, r * 0.75f, CrystalV)
    gem(center.x + r * 1.1f, center.y + r * 0.3f, r * 0.75f, CrystalP)
    // twinkles
    val tw = abs(sin(sparkle * Math.PI).toFloat())
    drawCircle(Color.White.copy(alpha = tw), r * 0.08f, Offset(center.x + r * 0.3f, center.y - r * 0.7f))
    drawCircle(Color.White.copy(alpha = 1 - tw), r * 0.06f, Offset(center.x - r * 0.5f, center.y + r * 0.2f))
}

private fun DrawScope.drawLightning(from: Offset, to: Offset, phase: Float) {
    val segs = 6
    val vx = to.x - from.x; val vy = to.y - from.y
    val len = kotlin.math.sqrt(vx * vx + vy * vy).coerceAtLeast(1f)
    val pxn = -vy / len; val pyn = vx / len            // perpendicular unit vector
    val amp = len * 0.18f
    val path = Path().apply { moveTo(from.x, from.y) }
    for (i in 1 until segs) {
        val f = i / segs.toFloat()
        val j = sin((phase * 40 + i * 1.7f).toDouble()).toFloat() * amp * (if (i % 2 == 0) 1f else -1f)
        path.lineTo(from.x + vx * f + pxn * j, from.y + vy * f + pyn * j)
    }
    path.lineTo(to.x, to.y)
    drawPath(path, CrystalC.copy(alpha = 0.45f), style = Stroke(width = 11f, cap = StrokeCap.Round))
    drawPath(path, Color.White, style = Stroke(width = 3.5f, cap = StrokeCap.Round))
    drawCircle(Color.White, 5f, to)
}

private fun DrawScope.drawSparks(at: Offset, r: Float) {
    for (i in 0..5) {
        val ang = i * 60.0
        val dx = (r * kotlin.math.cos(Math.toRadians(ang))).toFloat()
        val dy = (r * kotlin.math.sin(Math.toRadians(ang))).toFloat()
        drawCircle(Helmet.copy(alpha = 0.9f), r * 0.14f, Offset(at.x + dx, at.y + dy))
    }
}

/** A stylised zombie miner: green body, helmet with a lamp, swinging a pickaxe. */
private fun DrawScope.drawMiner(feet: Offset, h: Float, swing: Float, facingRight: Boolean) {
    val dir = if (facingRight) 1f else -1f
    translate(feet.x, feet.y) {
        scale(scaleX = dir, scaleY = 1f, pivot = Offset.Zero) {
            val bodyW = h * 0.55f
            val bodyH = h * 0.7f
            // legs
            drawRoundRect(ZombieDark, topLeft = Offset(-bodyW * 0.35f, -h * 0.18f),
                size = androidx.compose.ui.geometry.Size(bodyW * 0.28f, h * 0.18f),
                cornerRadius = androidx.compose.ui.geometry.CornerRadius(4f, 4f))
            drawRoundRect(ZombieDark, topLeft = Offset(bodyW * 0.08f, -h * 0.18f),
                size = androidx.compose.ui.geometry.Size(bodyW * 0.28f, h * 0.18f),
                cornerRadius = androidx.compose.ui.geometry.CornerRadius(4f, 4f))
            // body
            drawRoundRect(Brush.verticalGradient(listOf(Zombie, ZombieDark)),
                topLeft = Offset(-bodyW / 2, -h * 0.18f - bodyH),
                size = androidx.compose.ui.geometry.Size(bodyW, bodyH),
                cornerRadius = androidx.compose.ui.geometry.CornerRadius(bodyW * 0.3f, bodyW * 0.3f))
            // torn overalls strap
            drawRoundRect(Color(0xFF3B2F6B), topLeft = Offset(-bodyW * 0.1f, -h * 0.18f - bodyH),
                size = androidx.compose.ui.geometry.Size(bodyW * 0.2f, bodyH * 0.9f),
                cornerRadius = androidx.compose.ui.geometry.CornerRadius(3f, 3f))

            val headCy = -h * 0.18f - bodyH - h * 0.17f
            val headR = h * 0.2f
            // head
            drawCircle(Zombie, headR, Offset(0f, headCy))
            // eyes (glowing)
            drawCircle(Color(0xFFFFE14D), headR * 0.22f, Offset(-headR * 0.35f, headCy - headR * 0.1f))
            drawCircle(Color(0xFFFFE14D), headR * 0.22f, Offset(headR * 0.35f, headCy - headR * 0.1f))
            drawCircle(Color.Black, headR * 0.1f, Offset(-headR * 0.35f, headCy - headR * 0.1f))
            drawCircle(Color.Black, headR * 0.1f, Offset(headR * 0.35f, headCy - headR * 0.1f))
            // stitched mouth
            drawLine(Color.Black, Offset(-headR * 0.4f, headCy + headR * 0.45f),
                Offset(headR * 0.4f, headCy + headR * 0.45f), strokeWidth = 2.5f)
            // helmet + lamp
            val helmetTop = headCy - headR
            drawArc(Helmet, 180f, 180f, useCenter = true,
                topLeft = Offset(-headR * 1.1f, helmetTop - headR * 0.3f),
                size = androidx.compose.ui.geometry.Size(headR * 2.2f, headR * 1.6f))
            drawCircle(Color(0xFFFFF59D), headR * 0.28f, Offset(0f, helmetTop + headR * 0.1f)) // lamp
            // light beam from lamp toward crystals (forward)
            val beam = Path().apply {
                moveTo(headR * 0.2f, helmetTop + headR * 0.1f)
                lineTo(h * 1.1f, headCy + headR * 0.6f)
                lineTo(h * 1.1f, headCy - headR * 1.0f)
                close()
            }
            drawPath(beam, Brush.horizontalGradient(listOf(Color(0x55FFF59D), Color.Transparent)))

            // the miner GRIPS a pickaxe and swings the head down to strike forward
            val swingDeg = -82f + swing * 92f            // raised (back) -> struck (down/forward)
            val shoulder = Offset(bodyW * 0.30f, -h * 0.18f - bodyH * 0.72f)
            rotate(swingDeg, pivot = shoulder) {
                val hand = Offset(shoulder.x + h * 0.32f, shoulder.y + h * 0.04f)
                // forearm from shoulder to hand
                drawLine(ZombieDark, shoulder, hand, strokeWidth = h * 0.10f, cap = StrokeCap.Round)
                // wooden handle, held AT the hand and running forward to the head
                val handleBack = Offset(hand.x - h * 0.09f, hand.y - h * 0.03f)
                val tip = Offset(hand.x + h * 0.46f, hand.y + h * 0.13f)
                drawLine(Color(0xFF8B5A2B), handleBack, tip, strokeWidth = h * 0.06f, cap = StrokeCap.Round)
                // fist gripping the handle
                drawCircle(Color(0xFF2E5A12), h * 0.075f, hand)
                // steel pickaxe head (crescent) at the forward tip
                val head = Path().apply {
                    moveTo(tip.x - h * 0.16f, tip.y + h * 0.06f)
                    quadraticBezierTo(tip.x, tip.y - h * 0.20f, tip.x + h * 0.16f, tip.y + h * 0.06f)
                    quadraticBezierTo(tip.x, tip.y - h * 0.02f, tip.x - h * 0.16f, tip.y + h * 0.06f)
                    close()
                }
                drawPath(head, Brush.verticalGradient(listOf(Color(0xFFCFD8DC), Color(0xFF78909C))))
            }
        }
    }
}
