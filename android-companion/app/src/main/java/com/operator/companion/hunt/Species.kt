package com.operator.companion.hunt

import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.rotate
import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.sin

/** Body plan used to draw a species — minimal glowing 2D shapes (no 3D models: light on battery and GPU). */
enum class Body { SPIDER, CENTIPEDE, MOTH, RAT, SCORPION, BAT, FROG, MOLE, OVERLORD }

enum class Rarity(val label: String, val color: Color, val speed: Float) {
    COMMON("COMMON", Color(0xFF4ADE80), 0.6f),
    UNCOMMON("UNCOMMON", Color(0xFFFBBF24), 0.9f),
    RARE("RARE", Color(0xFFF43F5E), 1.25f),
    EPIC("EPIC", Color(0xFFA855F7), 1.5f),
    LEGENDARY("LEGENDARY", Color(0xFFFACC15), 1.8f),
}

/** How a species' weakness is exploited before its DNA can be taken — each uses a real phone sensor/action. */
enum class Challenge(val label: String, val how: String) {
    STEADY("STEADY AIM", "Hold the phone perfectly still"),
    DARK("DARKNESS", "Cover the light — it's blind in the dark"),
    LOW("UNDERSIDE", "Aim low — strike its exposed underside"),
    SHAKE("VIBRATION", "Shake the phone to send a ground pulse"),
    NOISE("SONIC", "Make a loud noise to disrupt it"),
    STEPS("CLOSE IN", "Walk closer — it can't hit at close range"),
    PULSE("ENERGY PULSE", "Fire an energy pulse (costs 5 💎)"),
    CORE("CRYSTAL CORE", "Tap its glowing crystal core 3 times"),
    RECOVER("RECOVERY", "Strike while it recovers after attacking"),
    FLANK("SLOW TURN", "Wait until it turns away, then strike"),
    ALL("DOMINION", "Needs the 9 original DNA signatures"),
}

data class Species(
    val id: String, val name: String, val dna: String, val rarity: Rarity, val body: Body,
    val power: String, val powerInfo: String, val weakness: String,
    val color: Color, val crystal: Color,
    val challenge: Challenge = Challenge.STEADY,
)

val SPECIES = listOf(
    Species("crystalclaw", "CRYSTALCLAW", "CCM-001", Rarity.COMMON, Body.SPIDER, "Crystal Charge",
        "Rushes at its target with crystal armour.", "Exposed underside", Color(0xFF1E3A8A), Color(0xFF60A5FA), Challenge.LOW),
    Species("shardfang", "SHARDFANG", "SDF-002", Rarity.COMMON, Body.SPIDER, "Web Trap",
        "Slows prey with a sticky web.", "Fire or an energy pulse", Color(0xFF4C1D95), Color(0xFFE879F9), Challenge.PULSE),
    Species("deepcrawler", "DEEPCRAWLER", "DPC-003", Rarity.UNCOMMON, Body.CENTIPEDE, "Burrow Dash",
        "Digs and strikes from underground.", "Ground vibration scanner", Color(0xFF1F2937), Color(0xFF38BDF8), Challenge.SHAKE),
    Species("glowwing", "GLOWWING", "GLW-004", Rarity.UNCOMMON, Body.MOTH, "Blinding Flare",
        "Disorients with a bright flash.", "Darkness", Color(0xFF6D28D9), Color(0xFF93C5FD), Challenge.DARK),
    Species("rockmaw", "ROCKMAW", "RKM-005", Rarity.UNCOMMON, Body.RAT, "Tunnelling Rush",
        "Burrows and strikes suddenly.", "Slow turning", Color(0xFF374151), Color(0xFF60A5FA), Challenge.FLANK),
    Species("venomshard", "VENOMSHARD", "VNS-006", Rarity.RARE, Body.SCORPION, "Venom Sting",
        "Poisons its target.", "Long recovery after an attack", Color(0xFF581C87), Color(0xFFD946EF), Challenge.RECOVER),
    Species("nightglider", "NIGHTGLIDER", "NTG-007", Rarity.RARE, Body.BAT, "Echo Dive",
        "Falls from above with a sonic strike.", "Sonic disruption", Color(0xFF312E81), Color(0xFFA78BFA), Challenge.NOISE),
    Species("toxicore", "TOXICORE", "TXC-008", Rarity.RARE, Body.FROG, "Toxic Spit",
        "Spits toxic acid.", "Limited attack range", Color(0xFF0F766E), Color(0xFF67E8F9), Challenge.STEPS),
    Species("earthreaper", "EARTHREAPER", "ETR-009", Rarity.EPIC, Body.MOLE, "Ground Slam",
        "Shakes the ground with massive force.", "Exposed crystal core", Color(0xFF3F2A1E), Color(0xFF818CF8), Challenge.CORE),
    Species("overlord", "CRYSTAL OVERLORD", "COV-010", Rarity.LEGENDARY, Body.OVERLORD, "Crystal Dominion",
        "Summons crystal shards and unleashes a nova.", "All nine other DNA signatures combined",
        Color(0xFF2E1065), Color(0xFFF472B6), Challenge.ALL),
    // ---- 40 more organisms (unlock gradually as you collect) ----
    Species("frostmite", "FROSTMITE", "FRO-011", Rarity.COMMON, Body.SPIDER, "Frost Bite",
        "Freezes what it bites.", "Warmth — hold steady near it", Color(0xFF0F2A44), Color(0xFF7DD3FC), Challenge.STEADY),
    Species("embertick", "EMBERTICK", "EMB-012", Rarity.COMMON, Body.SPIDER, "Cinder Hop",
        "Leaps in bursts of embers.", "Smothered in darkness", Color(0xFF3B1206), Color(0xFFFB923C), Challenge.DARK),
    Species("quartzbeetle", "QUARTZBEETLE", "QUA-013", Rarity.COMMON, Body.SCORPION, "Shell Wall",
        "Hides behind a quartz shell.", "Soft belly", Color(0xFF1E293B), Color(0xFFE2E8F0), Challenge.LOW),
    Species("glimmerfly", "GLIMMERFLY", "GLI-014", Rarity.COMMON, Body.MOTH, "Glitter Dust",
        "Blinds with sparkling dust.", "Darkness", Color(0xFF4C1D95), Color(0xFFF0ABFC), Challenge.DARK),
    Species("mudskitter", "MUDSKITTER", "MUD-015", Rarity.COMMON, Body.CENTIPEDE, "Mud Slide",
        "Slides away through mud.", "Ground vibration", Color(0xFF3F2A1E), Color(0xFFA3E635), Challenge.SHAKE),
    Species("pebblemouse", "PEBBLEMOUSE", "PEB-016", Rarity.COMMON, Body.RAT, "Pebble Roll",
        "Curls into a rolling pebble.", "Slow to uncurl", Color(0xFF44403C), Color(0xFFFDE68A), Challenge.RECOVER),
    Species("sparkgnat", "SPARKGNAT", "SPA-017", Rarity.COMMON, Body.MOTH, "Static Zip",
        "Zips around on static charge.", "Loud noise scatters its charge", Color(0xFF1E3A8A), Color(0xFFFDE047), Challenge.NOISE),
    Species("dustcrawler", "DUSTCRAWLER", "DUS-018", Rarity.COMMON, Body.CENTIPEDE, "Dust Cloud",
        "Hides in a cloud of dust.", "Short sight — get close", Color(0xFF57534E), Color(0xFFD6D3D1), Challenge.STEPS),
    Species("cavetoad", "CAVETOAD", "CAV-019", Rarity.COMMON, Body.FROG, "Tongue Lash",
        "Snaps from a distance.", "Can't reach up close", Color(0xFF14532D), Color(0xFF86EFAC), Challenge.STEPS),
    Species("ashmoth", "ASHMOTH", "ASH-020", Rarity.COMMON, Body.MOTH, "Ash Veil",
        "Vanishes behind ash.", "Steady aim cuts through", Color(0xFF292524), Color(0xFFA8A29E), Challenge.STEADY),
    Species("copperwasp", "COPPERWASP", "COP-021", Rarity.UNCOMMON, Body.BAT, "Copper Sting",
        "Stings with a charged tail.", "Long recovery after stinging", Color(0xFF78350F), Color(0xFFFBBF24), Challenge.RECOVER),
    Species("tidecrab", "TIDECRAB", "TID-022", Rarity.UNCOMMON, Body.SCORPION, "Tide Pinch",
        "Pinches with water force.", "Exposed underside", Color(0xFF0C4A6E), Color(0xFF38BDF8), Challenge.LOW),
    Species("thornback", "THORNBACK", "THO-023", Rarity.UNCOMMON, Body.RAT, "Thorn Burst",
        "Fires thorns from its back.", "Slow turning", Color(0xFF365314), Color(0xFFBEF264), Challenge.FLANK),
    Species("mosslurk", "MOSSLURK", "MOS-024", Rarity.UNCOMMON, Body.FROG, "Spore Puff",
        "Puffs sleeping spores.", "Energy pulse burns spores", Color(0xFF1A2E05), Color(0xFF84CC16), Challenge.PULSE),
    Species("zapworm", "ZAPWORM", "ZAP-025", Rarity.UNCOMMON, Body.CENTIPEDE, "Arc Coil",
        "Coils with electric arcs.", "Grounded by vibration", Color(0xFF1E1B4B), Color(0xFF22D3EE), Challenge.SHAKE),
    Species("ghostweb", "GHOSTWEB", "GHO-026", Rarity.UNCOMMON, Body.SPIDER, "Phase Web",
        "Weaves webs that phase out.", "Fire or energy pulse", Color(0xFF1F2937), Color(0xFFE5E7EB), Challenge.PULSE),
    Species("blinkbat", "BLINKBAT", "BLI-027", Rarity.UNCOMMON, Body.BAT, "Blink Step",
        "Teleports short distances.", "Sonic disruption", Color(0xFF312E81), Color(0xFFC4B5FD), Challenge.NOISE),
    Species("sandmole", "SANDMOLE", "SAN-028", Rarity.UNCOMMON, Body.MOLE, "Sand Spout",
        "Sprays blinding sand.", "Exposed crystal core", Color(0xFF713F12), Color(0xFFFCD34D), Challenge.CORE),
    Species("shadefox", "SHADEFOX", "SHA-029", Rarity.UNCOMMON, Body.RAT, "Shade Dash",
        "Dashes between shadows.", "Darkness traps it", Color(0xFF18181B), Color(0xFFA78BFA), Challenge.DARK),
    Species("coralshard", "CORALSHARD", "COR-030", Rarity.UNCOMMON, Body.SCORPION, "Reef Spike",
        "Grows coral spikes.", "Steady aim", Color(0xFF831843), Color(0xFFF9A8D4), Challenge.STEADY),
    Species("lavascuttle", "LAVASCUTTLE", "LAV-031", Rarity.RARE, Body.SCORPION, "Magma Pinch",
        "Pinches with molten claws.", "Long recovery after attack", Color(0xFF7F1D1D), Color(0xFFF97316), Challenge.RECOVER),
    Species("stormwing", "STORMWING", "STO-032", Rarity.RARE, Body.BAT, "Thunder Dive",
        "Dives with a thunderclap.", "Sonic disruption", Color(0xFF0F172A), Color(0xFF60A5FA), Challenge.NOISE),
    Species("ironhide", "IRONHIDE", "IRO-033", Rarity.RARE, Body.MOLE, "Iron Charge",
        "Charges with iron skin.", "Slow turning", Color(0xFF27272A), Color(0xFFA1A1AA), Challenge.FLANK),
    Species("prismspider", "PRISMSPIDER", "PRI-034", Rarity.RARE, Body.SPIDER, "Prism Beam",
        "Splits light into beams.", "Darkness", Color(0xFF3B0764), Color(0xFFF0ABFC), Challenge.DARK),
    Species("bogfiend", "BOGFIEND", "BOG-035", Rarity.RARE, Body.FROG, "Bog Pull",
        "Drags prey into mud.", "Limited range — close in", Color(0xFF1C1917), Color(0xFF4ADE80), Challenge.STEPS),
    Species("riftmoth", "RIFTMOTH", "RIF-036", Rarity.RARE, Body.MOTH, "Rift Flutter",
        "Flutters through rifts.", "Energy pulse seals rifts", Color(0xFF2E1065), Color(0xFF818CF8), Challenge.PULSE),
    Species("bonecrusher", "BONECRUSHER", "BON-037", Rarity.RARE, Body.RAT, "Bone Crush",
        "Bites through stone.", "Exposed underside", Color(0xFF44403C), Color(0xFFF5F5F4), Challenge.LOW),
    Species("quakeworm", "QUAKEWORM", "QUA-038", Rarity.RARE, Body.CENTIPEDE, "Quake Burrow",
        "Shakes tunnels loose.", "Counter-vibration", Color(0xFF422006), Color(0xFFEAB308), Challenge.SHAKE),
    Species("solarwing", "SOLARWING", "SOL-039", Rarity.RARE, Body.MOTH, "Solar Flare",
        "Burns with stored sunlight.", "Darkness", Color(0xFF713F12), Color(0xFFFDE047), Challenge.DARK),
    Species("howlrat", "HOWLRAT", "HOW-040", Rarity.RARE, Body.RAT, "Sonic Howl",
        "Howls to stun.", "Louder noise overloads it", Color(0xFF1E1B4B), Color(0xFFE879F9), Challenge.NOISE),
    Species("obsidian_titan", "OBSIDIAN TITAN", "OBS-041", Rarity.EPIC, Body.MOLE, "Obsidian Wall",
        "Raises walls of glass rock.", "Exposed crystal core", Color(0xFF09090B), Color(0xFFA855F7), Challenge.CORE),
    Species("voidweaver", "VOIDWEAVER", "VOI-042", Rarity.EPIC, Body.SPIDER, "Void Web",
        "Webs that swallow light.", "Energy pulse", Color(0xFF020617), Color(0xFF6366F1), Challenge.PULSE),
    Species("thunderback", "THUNDERBACK", "THU-043", Rarity.EPIC, Body.FROG, "Thunder Leap",
        "Leaps with a shockwave.", "Long recovery after landing", Color(0xFF1E3A8A), Color(0xFFFACC15), Challenge.RECOVER),
    Species("mirage_manta", "MIRAGE MANTA", "MIR-044", Rarity.EPIC, Body.BAT, "Mirage Glide",
        "Glides as a mirage.", "Steady aim reveals it", Color(0xFF083344), Color(0xFF67E8F9), Challenge.STEADY),
    Species("magma_drake", "MAGMA DRAKE", "MAG-045", Rarity.EPIC, Body.SCORPION, "Lava Breath",
        "Breathes molten rock.", "Slow turning", Color(0xFF450A0A), Color(0xFFF97316), Challenge.FLANK),
    Species("chronomoth", "CHRONOMOTH", "CHR-046", Rarity.EPIC, Body.MOTH, "Time Slip",
        "Slips a second back in time.", "Sonic disruption", Color(0xFF1E1B4B), Color(0xFFFDE68A), Challenge.NOISE),
    Species("abyss_lurker", "ABYSS LURKER", "ABY-047", Rarity.LEGENDARY, Body.CENTIPEDE, "Abyss Gate",
        "Opens a pit to the deep.", "Exposed crystal core", Color(0xFF030712), Color(0xFF22D3EE), Challenge.CORE),
    Species("starfall_roc", "STARFALL ROC", "STA-048", Rarity.LEGENDARY, Body.BAT, "Meteor Dive",
        "Dives like a falling star.", "Darkness hides the stars", Color(0xFF0C0A09), Color(0xFFFDE047), Challenge.DARK),
    Species("titan_maw", "TITAN MAW", "TIT-049", Rarity.LEGENDARY, Body.RAT, "World Bite",
        "Bites through the mine itself.", "Slow turning", Color(0xFF1C1917), Color(0xFFFB7185), Challenge.FLANK),
    Species("primordial_queen", "PRIMORDIAL QUEEN", "PRI-050", Rarity.LEGENDARY, Body.OVERLORD, "Hive Command",
        "Commands every crawler in the mine.", "Energy pulse", Color(0xFF3B0764), Color(0xFF4ADE80), Challenge.PULSE),
)

private fun DrawScope.crystals(at: Offset, s: Float, c: Color, n: Int, spread: Float, t: Float) {
    for (i in 0 until n) {
        val f = if (n == 1) 0f else i / (n - 1f) - 0.5f
        val base = at + Offset(f * spread, 0f)
        val h = s * (0.55f + 0.35f * ((i * 37 % 10) / 10f)) * (1f + 0.06f * sin(t * 3 + i))
        val w = s * 0.16f
        val p = Path().apply { moveTo(base.x - w, base.y); lineTo(base.x + f * w * 2, base.y - h); lineTo(base.x + w, base.y); close() }
        drawPath(p, Brush.verticalGradient(listOf(Color.White.copy(alpha = 0.9f), c, c.copy(alpha = 0.6f)), base.y - h, base.y))
    }
}

private fun DrawScope.legs(c: Offset, s: Float, n: Int, len: Float, col: Color, t: Float, span: Float = 0.9f) {
    for (side in listOf(-1f, 1f)) for (i in 0 until n) {
        val a = (-span / 2 + span * i / (n - 1f)) + sin(t * 9 + i * 1.7f) * 0.12f
        val knee = c + Offset(side * cos(a) * len * 0.6f, -s * 0.15f + sin(a) * len * 0.5f)
        val foot = c + Offset(side * cos(a) * len, s * 0.35f + sin(a) * len * 0.6f)
        drawLine(col, c, knee, strokeWidth = s * 0.06f, cap = StrokeCap.Round)
        drawLine(col, knee, foot, strokeWidth = s * 0.05f, cap = StrokeCap.Round)
    }
}

/** Draws one species centred at [c], [s] ≈ half its height in px. [t] = time (s) for idle animation. */
fun DrawScope.drawSpecies(sp: Species, c: Offset, s: Float, t: Float, facingLeft: Boolean = false) {
    val glow = sp.crystal
    drawCircle(Brush.radialGradient(listOf(glow.copy(alpha = 0.35f), Color.Transparent), c, s * 1.9f), s * 1.9f, c)
    val body = sp.color
    val dark = Color(0xFF0B0F1A)
    val flip = if (facingLeft) -1f else 1f
    when (sp.body) {
        Body.SPIDER, Body.OVERLORD -> {
            val k = if (sp.body == Body.OVERLORD) 1.3f else 1f
            legs(c, s * k, 4, s * 1.25f * k, dark.copy(alpha = 0.95f), t)
            legs(c, s * k, 4, s * 1.2f * k, glow.copy(alpha = 0.55f), t + 0.3f)
            drawOval(Brush.radialGradient(listOf(body.copy(alpha = 1f), dark), c, s), Offset(c.x - s * 0.7f * k, c.y - s * 0.45f * k), Size(s * 1.4f * k, s * 0.95f * k))
            drawCircle(body, s * 0.35f * k, c + Offset(flip * s * 0.75f * k, s * 0.05f))
            crystals(c + Offset(0f, -s * 0.3f * k), s * k, glow, if (sp.body == Body.OVERLORD) 7 else 5, s * 0.9f * k, t)
            val eye = if (sp.body == Body.OVERLORD) Color(0xFFF0ABFC) else Color(0xFFEF4444)
            drawCircle(eye, s * 0.07f * k, c + Offset(flip * s * 0.95f * k, -s * 0.02f))
            drawCircle(eye, s * 0.07f * k, c + Offset(flip * s * 0.82f * k, s * 0.12f))
            if (sp.body == Body.OVERLORD) {   // crystal blade arms + crown aura
                for (side in listOf(-1f, 1f)) rotate(side * (35f + 8f * sin(t * 4)), c) {
                    drawLine(glow, c, c + Offset(side * s * 1.9f, -s * 1.1f), s * 0.12f, StrokeCap.Round)
                }
                drawCircle(glow.copy(alpha = 0.5f), s * (1.7f + 0.1f * sin(t * 2)), c, style = Stroke(s * 0.05f))
            }
        }
        Body.CENTIPEDE -> {
            for (i in 0 until 9) {
                val x = c.x + flip * (i - 4) * s * 0.32f
                val y = c.y + sin(t * 6 + i * 0.8f) * s * 0.12f
                drawLine(dark, Offset(x, y), Offset(x, y + s * 0.45f), s * 0.05f, StrokeCap.Round)
                drawCircle(Brush.radialGradient(listOf(body, dark), Offset(x, y), s * 0.3f), s * 0.24f, Offset(x, y))
                if (i % 2 == 0) crystals(Offset(x, y - s * 0.15f), s * 0.5f, glow, 1, 0f, t + i)
            }
            drawCircle(Color(0xFFFBBF24), s * 0.06f, c + Offset(flip * s * 1.42f, -s * 0.05f))
        }
        Body.MOTH -> {
            val flap = 0.55f + 0.45f * sin(t * 10)
            for (side in listOf(-1f, 1f)) {
                val p = Path().apply {
                    moveTo(c.x, c.y)
                    cubicTo(c.x + side * s * 1.6f * flap, c.y - s * 1.5f, c.x + side * s * 2.0f * flap, c.y + s * 0.3f, c.x, c.y + s * 0.2f)
                }
                drawPath(p, Brush.radialGradient(listOf(Color.White.copy(alpha = 0.8f), glow, body.copy(alpha = 0.7f)), c, s * 2f))
                drawPath(p, Color.White.copy(alpha = 0.6f), style = Stroke(s * 0.03f))
            }
            drawOval(dark, Offset(c.x - s * 0.1f, c.y - s * 0.45f), Size(s * 0.2f, s * 1.0f))
        }
        Body.RAT, Body.MOLE -> {
            val k = if (sp.body == Body.MOLE) 1.2f else 1f
            val step = sin(t * 8) * s * 0.08f
            drawLine(Color(0xFFCBD5E1).copy(alpha = 0.7f), c + Offset(-flip * s * 0.9f * k, s * 0.1f),
                c + Offset(-flip * s * 1.8f * k, s * (0.3f + 0.2f * sin(t * 3))), s * 0.06f, StrokeCap.Round)
            drawOval(Brush.radialGradient(listOf(body.copy(alpha = 1f), dark), c, s * 1.2f * k),
                Offset(c.x - s * 1.0f * k, c.y - s * 0.55f * k), Size(s * 2.0f * k, s * 1.1f * k))
            drawCircle(body, s * 0.42f * k, c + Offset(flip * s * 1.0f * k, s * 0.05f))
            drawCircle(Color(0xFFEF4444), s * 0.07f, c + Offset(flip * s * 1.15f * k, -s * 0.08f))
            for (dx in listOf(-0.5f, 0.6f)) drawLine(dark, c + Offset(dx * s * k, s * 0.4f * k), c + Offset(dx * s * k + step, s * 0.75f * k), s * 0.1f, StrokeCap.Round)
            if (sp.body == Body.MOLE) for (i in 0 until 3) drawLine(Color(0xFFE5E7EB), c + Offset(flip * s * 1.2f, s * 0.3f),
                c + Offset(flip * s * (1.55f + i * 0.08f), s * (0.55f + i * 0.12f)), s * 0.06f, StrokeCap.Round)
            crystals(c + Offset(-flip * s * 0.1f, -s * 0.4f * k), s * k, glow, 5, s * 1.2f * k, t)
        }
        Body.SCORPION -> {
            legs(c, s, 4, s * 1.0f, dark, t, 0.7f)
            for (i in 0 until 4) drawCircle(body, s * (0.4f - i * 0.05f), c + Offset(-flip * s * (0.3f + i * 0.35f), 0f))
            // tail curls up and over the back; the stinger sways
            val tail = listOf(Offset(1.4f, -0.4f), Offset(1.55f, -1.0f), Offset(1.2f, -1.5f), Offset(0.6f, -1.55f + 0.15f * sin(t * 5)))
            tail.forEach { o -> drawCircle(body, s * 0.2f, c + Offset(-flip * o.x * s, o.y * s)) }
            drawCircle(glow, s * 0.16f, c + Offset(-flip * 0.3f * s, (-1.6f + 0.15f * sin(t * 5)) * s))
            for (side in listOf(-1f, 1f)) drawCircle(glow.copy(alpha = 0.8f), s * 0.18f, c + Offset(flip * s * 1.1f, side * s * 0.35f))
        }
        Body.BAT -> {
            val flap = 0.6f + 0.4f * sin(t * 12)
            for (side in listOf(-1f, 1f)) {
                val p = Path().apply {
                    moveTo(c.x, c.y)
                    lineTo(c.x + side * s * 1.0f, c.y - s * 0.9f * flap)
                    lineTo(c.x + side * s * 2.0f, c.y - s * 0.4f * flap)
                    lineTo(c.x + side * s * 1.6f, c.y + s * 0.2f)
                    lineTo(c.x + side * s * 1.1f, c.y)
                    lineTo(c.x + side * s * 0.6f, c.y + s * 0.3f)
                    close()
                }
                drawPath(p, Brush.radialGradient(listOf(glow, body.copy(alpha = 0.8f)), c, s * 2f))
            }
            drawOval(dark, Offset(c.x - s * 0.25f, c.y - s * 0.35f), Size(s * 0.5f, s * 0.75f))
            drawCircle(Color(0xFFF0ABFC), s * 0.05f, c + Offset(-s * 0.08f, -s * 0.15f))
            drawCircle(Color(0xFFF0ABFC), s * 0.05f, c + Offset(s * 0.08f, -s * 0.15f))
        }
        Body.FROG -> {
            val hop = -kotlin.math.abs(sin(t * 3)) * s * 0.35f
            val cc = c + Offset(0f, hop)
            for (side in listOf(-1f, 1f)) drawOval(Color(0xFFF59E0B).copy(alpha = 0.9f), Offset(cc.x + side * s * 0.6f - s * 0.35f, cc.y + s * 0.2f), Size(s * 0.7f, s * 0.4f))
            drawOval(Brush.radialGradient(listOf(Color(0xFF34D399), body), cc, s * 1.1f), Offset(cc.x - s * 0.9f, cc.y - s * 0.6f), Size(s * 1.8f, s * 1.1f))
            for (side in listOf(-1f, 1f)) {
                drawCircle(Color(0xFFFDE68A), s * 0.2f, cc + Offset(side * s * 0.45f, -s * 0.55f))
                drawCircle(dark, s * 0.09f, cc + Offset(side * s * 0.45f, -s * 0.55f))
            }
            crystals(cc + Offset(0f, -s * 0.45f), s * 0.8f, glow, 4, s * 0.8f, t)
        }
    }
}

fun speciesById(id: String?) = SPECIES.firstOrNull { it.id == id }
fun twoPi() = (2 * PI).toFloat()
