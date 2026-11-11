package com.operator.companion.hunt

import androidx.activity.compose.BackHandler
import androidx.compose.animation.core.*
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

private val Bg = Color(0xFF07081A)
private val Card = Color(0xFF12132E)
private val Line = Color(0xFF2E2A6B)
private val Cyan = Color(0xFF22D3EE)
private val Violet = Color(0xFFA855F7)
private val Gold = Color(0xFFFACC15)
private val Muted = Color(0xFF94A3B8)

/** Shared clock for the little idle animations of the creature tiles. */
@Composable
private fun rememberClock(): Float {
    var t by remember { mutableFloatStateOf(0f) }
    LaunchedEffect(Unit) { val t0 = withFrameNanos { it }; while (true) withFrameNanos { t = (it - t0) / 1e9f } }
    return t
}

/** Crystal Mines home: mission brief, the escaped organisms, rewards and the big HUNT button. */
@Composable
fun MinesHomeScreen(onHunt: () -> Unit, onBestiary: () -> Unit) {
    val collected by HuntStore.collected.collectAsState()
    val shards by HuntStore.shards.collectAsState()
    val xp by HuntStore.xp.collectAsState()
    val muted by HuntStore.muted.collectAsState()
    val ctx = androidx.compose.ui.platform.LocalContext.current
    val t = rememberClock()
    // the common Crystal Mines tune plays on the home screen
    DisposableEffect(muted) { if (!muted) HuntSound.music(HuntSound.Music.THEME); onDispose { HuntSound.music(HuntSound.Music.NONE) } }
    val pulse by rememberInfiniteTransition(label = "hunt").animateFloat(1f, 1.05f, infiniteRepeatable(tween(900), RepeatMode.Reverse), label = "p")
    Column(Modifier.fillMaxSize().background(Brush.verticalGradient(listOf(Color(0xFF1A1442), Bg)))
        .verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
        // title
        Column(Modifier.fillMaxWidth(), horizontalAlignment = Alignment.CenterHorizontally) {
            Text("CRYSTAL MINES", color = Color.White, fontSize = 30.sp, fontWeight = FontWeight.Black, letterSpacing = 3.sp)
            Text("/// CONTAINMENT BREACH ///", color = Violet, fontSize = 12.sp, fontWeight = FontWeight.Bold, letterSpacing = 3.sp)
            Spacer(Modifier.height(6.dp))
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(14.dp)) {
                Text("🧬 ${collected.size}/${SPECIES.size}", color = Cyan, fontSize = 14.sp, fontWeight = FontWeight.Black)
                Text("💎 $shards", color = Color(0xFFE879F9), fontSize = 14.sp, fontWeight = FontWeight.Black)
                Text("⭐ LV ${HuntStore.level(xp)}", color = Gold, fontSize = 14.sp, fontWeight = FontWeight.Black)
                Text(if (muted) "🔇" else "🔊", fontSize = 16.sp, modifier = Modifier.clickable { HuntStore.toggleMute(ctx) })
            }
            Box(Modifier.padding(top = 6.dp).fillMaxWidth(0.7f).height(6.dp).clip(RoundedCornerShape(3.dp)).background(Line)) {
                Box(Modifier.fillMaxWidth(collected.size / SPECIES.size.toFloat()).fillMaxHeight().background(Brush.horizontalGradient(listOf(Cyan, Violet))))
            }
        }
        // mission brief
        Panel("🎯 MISSION BRIEF") {
            Text("While digging a rare crystal vein, the miners broke into a sealed cavern… ", color = Color(0xFFE2E8F0), fontSize = 13.sp)
            Text("${SPECIES.size} mutated creatures escaped into the real world. Open the HUNT camera, track them on foot — they run! — exploit each weakness and capture their DNA. Rarer ones appear as your collection grows.",
                color = Color(0xFFE2E8F0), fontSize = 13.sp)
        }
        // organisms: big cards, grouped by mutation series (2 per row)
        Panel("🐾 ESCAPED ORGANISMS (${SPECIES.size})", action = "Bestiary ›", onAction = onBestiary) {
            SPECIES.groupBy { it.series }.forEach { (series, list) ->
                SeriesHeader(series, list.count { it.id in collected }, list.size)
                list.chunked(2).forEach { row ->
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        row.forEach { sp -> SpeciesCard(sp, sp.id in collected, t, Modifier.weight(1f), onBestiary) }
                        if (row.size == 1) Spacer(Modifier.weight(1f))
                    }
                    Spacer(Modifier.height(8.dp))
                }
            }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            Panel("🏆 REWARDS", Modifier.weight(1f)) {
                Text("🧬 DNA samples", color = Color(0xFFE2E8F0), fontSize = 12.sp)
                Text("💎 Crystal shards (pulse ammo)", color = Color(0xFFE2E8F0), fontSize = 12.sp)
                Text("⭐ XP · LV ${HuntStore.level(xp)}", color = Color(0xFFE2E8F0), fontSize = 12.sp)
                Box(Modifier.fillMaxWidth().height(5.dp).clip(RoundedCornerShape(3.dp)).background(Line)) {
                    Box(Modifier.fillMaxWidth(HuntStore.levelProgress(xp)).fillMaxHeight().background(Gold))
                }
            }
            Panel("⚠ CAUTION", Modifier.weight(1f), border = Color(0xFFF43F5E)) {
                Text("They flee when spotted.", color = Color(0xFFFECACA), fontSize = 12.sp)
                Text("Walk to close in; SCAN when lost.", color = Color(0xFFFECACA), fontSize = 12.sp)
                Text("Every one has a weakness.", color = Color(0xFFFECACA), fontSize = 12.sp)
            }
        }
        // HUNT
        Box(Modifier.fillMaxWidth().scale(pulse).clip(RoundedCornerShape(18.dp))
            .background(Brush.horizontalGradient(listOf(Color(0xFFF59E0B), Gold))).clickable(onClick = onHunt).padding(vertical = 18.dp),
            contentAlignment = Alignment.Center) {
            Text("🎯  HUNT  »", color = Color(0xFF1C1206), fontSize = 24.sp, fontWeight = FontWeight.Black, letterSpacing = 3.sp)
        }
        Text("HUNT · COLLECT · SURVIVE", color = Muted, fontSize = 11.sp, letterSpacing = 3.sp, modifier = Modifier.fillMaxWidth(), textAlign = TextAlign.Center)
        Spacer(Modifier.height(8.dp))
    }
}

/** Bestiary: every organism with its power and weakness; uncollected ones are silhouettes. */
@Composable
fun BestiaryScreen(onBack: () -> Unit) {
    BackHandler(onBack = onBack)
    val collected by HuntStore.collected.collectAsState()
    val t = rememberClock()
    Column(Modifier.fillMaxSize().background(Bg)) {
        Row(Modifier.fillMaxWidth().padding(14.dp), verticalAlignment = Alignment.CenterVertically) {
            Text("←", color = Color.White, fontSize = 22.sp, modifier = Modifier.clickable(onClick = onBack).padding(end = 12.dp))
            Column(Modifier.weight(1f)) {
                Text("BESTIARY", color = Color.White, fontSize = 20.sp, fontWeight = FontWeight.Black, letterSpacing = 2.sp)
                Text("Track · Hunt · Collect", color = Violet, fontSize = 11.sp)
            }
            Text("🧬 ${collected.size}/${SPECIES.size}", color = Cyan, fontWeight = FontWeight.Black)
        }
        Column(Modifier.verticalScroll(rememberScrollState()).padding(horizontal = 14.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            SPECIES.groupBy { it.series }.forEach { (series, list) ->
              SeriesHeader(series, list.count { it.id in collected }, list.size)
              list.forEach { sp ->
                val i = SPECIES.indexOf(sp)
                val got = sp.id in collected
                Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(16.dp)).background(Brush.horizontalGradient(listOf(Color(0xFF181540), Card)))
                    .border(1.dp, if (got) sp.rarity.color.copy(alpha = 0.85f) else Line, RoundedCornerShape(16.dp)).padding(10.dp),
                    verticalAlignment = Alignment.CenterVertically) {
                    Canvas(Modifier.size(128.dp).alpha(if (got) 1f else 0.8f)) { drawSpecies(sp, center, size.minDimension * 0.27f, t + i) }
                    Spacer(Modifier.width(10.dp))
                    Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(2.dp)) {
                        Text(sp.rarity.label, color = sp.rarity.color, fontSize = 10.sp, fontWeight = FontWeight.Black, letterSpacing = 1.sp)
                        Text("${i + 1}. ${if (got) sp.name else "UNKNOWN"}", color = Color.White, fontWeight = FontWeight.Black, fontSize = 17.sp)
                        Text("DNA: ${sp.dna}", color = Muted, fontSize = 11.sp)
                        Text("⚡ ${sp.power} — ${sp.powerInfo}", color = Color(0xFFE2E8F0), fontSize = 12.sp)
                        Text("🎯 Weakness: ${if (got) sp.weakness else "capture its DNA to learn it"}", color = if (got) Gold else Muted, fontSize = 12.sp)
                        Text("🧪 ${sp.challenge.label}", color = Cyan, fontSize = 11.sp, fontWeight = FontWeight.Bold)
                    }
                }
                Spacer(Modifier.height(8.dp))
              }
            }
            Spacer(Modifier.height(16.dp))
        }
    }
}

@Composable
private fun Panel(title: String, modifier: Modifier = Modifier, border: Color = Line, action: String? = null, onAction: () -> Unit = {},
                  content: @Composable ColumnScope.() -> Unit) {
    Column(modifier.fillMaxWidth().clip(RoundedCornerShape(16.dp)).background(Card).border(1.dp, border, RoundedCornerShape(16.dp)).padding(12.dp),
        verticalArrangement = Arrangement.spacedBy(6.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(title, color = Color.White, fontSize = 13.sp, fontWeight = FontWeight.Black, letterSpacing = 1.sp, modifier = Modifier.weight(1f))
            if (action != null) Text(action, color = Cyan, fontSize = 12.sp, fontWeight = FontWeight.Bold, modifier = Modifier.clickable(onClick = onAction))
        }
        content()
    }
}

private val SERIES_NAMES = mapOf(1 to "ESCAPED ORGANISMS", 2 to "MUTATION SERIES · II", 3 to "MUTATION SERIES · III", 4 to "MUTATION SERIES · IV", 5 to "MUTATION SERIES · V")

@Composable
private fun SeriesHeader(series: Int, got: Int, total: Int) {
    Row(Modifier.fillMaxWidth().padding(top = 6.dp, bottom = 6.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(SERIES_NAMES[series] ?: "SERIES $series", color = Violet, fontSize = 12.sp, fontWeight = FontWeight.Black, letterSpacing = 2.sp)
        Spacer(Modifier.width(8.dp))
        Box(Modifier.weight(1f).height(1.dp).background(Line))
        Spacer(Modifier.width(8.dp))
        Text("$got / $total", color = Cyan, fontSize = 11.sp, fontWeight = FontWeight.Bold)
    }
}

/** A big bestiary card: the creature with its aura, name, DNA, rarity and power. */
@Composable
private fun SpeciesCard(sp: Species, got: Boolean, t: Float, modifier: Modifier, onClick: () -> Unit) {
    Column(modifier.clip(RoundedCornerShape(14.dp)).background(Brush.verticalGradient(listOf(Color(0xFF181540), Color(0xFF0B0C22))))
        .border(1.5.dp, if (got) sp.rarity.color else sp.rarity.color.copy(alpha = 0.35f), RoundedCornerShape(14.dp)).clickable(onClick = onClick).padding(8.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("${SPECIES.indexOf(sp) + 1}.", color = Muted, fontSize = 10.sp, fontWeight = FontWeight.Bold)
            Spacer(Modifier.weight(1f))
            Text(sp.rarity.label, color = sp.rarity.color, fontSize = 9.sp, fontWeight = FontWeight.Black)
        }
        Canvas(Modifier.fillMaxWidth().height(110.dp).alpha(if (got) 1f else 0.8f)) { drawSpecies(sp, center, size.minDimension * 0.27f, t + sp.dna.hashCode() % 7) }
        Text(if (got) sp.name else "???", color = Color.White, fontSize = 12.sp, fontWeight = FontWeight.Black, maxLines = 2, lineHeight = 14.sp)
        Text("DNA: ${sp.dna}", color = Muted, fontSize = 10.sp)
        Text("⚡ ${sp.power}", color = Gold, fontSize = 11.sp, fontWeight = FontWeight.Bold, maxLines = 1)
    }
}
