package com.operator.companion.pb

import androidx.compose.ui.graphics.Color
import org.json.JSONObject

/**
 * Mood-reactive chat theme (same rules as the laptop's pb-mood.js): scores the last few messages by
 * keywords + emoji, newest weighted most, and picks a mood whose palettes the chat cycles through —
 * angry → demon / frost / inferno red, happy → neon green / purple, love → rose, sad → midnight blue.
 */
object ChatMood {
    /** a, b = own-bubble gradient (white text stays ≥ 4.5:1); accent tints everything else. */
    data class Palette(val a: Color, val b: Color, val accent: Color)

    private val LEX = mapOf(
        "angry" to listOf("angry", "mad", "hate", "furious", "annoyed", "wtf", "stupid", "idiot", "damn", "shut up", "useless",
            "pissed", "rage", "worst", "irritat", "gussa", "bakwas", "pagal", "bekar", "😡", "🤬", "😠", "👿", "💢", "😤"),
        "happy" to listOf("happy", "great", "awesome", "nice", "cool", "good", "thanks", "thank you", "yay", "lol", "haha", "done",
            "perfect", "amazing", "excellent", "mast", "badhiya", "shandar", "😂", "😄", "😁", "😊", "👍", "🎉", "✅", "🤣"),
        "love" to listOf("love", "miss you", "cute", "sweet", "dear", "babe", "pyaar", "❤", "😍", "🥰", "😘", "💕", "💖", "💜"),
        "sad" to listOf("sad", "sorry", "cry", "upset", "tired", "alone", "hurt", "bad day", "miss", "unfortunately", "dukh",
            "😢", "😭", "😞", "😔", "💔", "🥺"),
        "hype" to listOf("lets go", "let's go", "fire", "insane", "crazy", "epic", "boost", "win", "won", "rank", "legend", "beast",
            "🔥", "🚀", "⚡", "💥", "🏆", "💯", "!!!"),
    )

    private fun p(a: Long, b: Long, ac: Long) = Palette(Color(a), Color(b), Color(ac))
    // Muted, low-glare tones so long chats stay comfortable; same values as pb-mood.js
    val PALETTES = mapOf(
        "neutral" to listOf(p(0xFF4C4F9E, 0xFF3D5F9E, 0xFF8F94E0), p(0xFF46588F, 0xFF3C6A80, 0xFF86A8D8), p(0xFF5A4B92, 0xFF4A578F, 0xFFA399DC)),
        "angry" to listOf(p(0xFF8E2F36, 0xFF7A3B2E, 0xFFE0787A), p(0xFF8A3346, 0xFF6B3A55, 0xFFE58A9B), p(0xFF7A2A2A, 0xFF8A4428, 0xFFE08A6A), p(0xFF6E2228, 0xFF5E2A3E, 0xFFD46A72)),
        "happy" to listOf(p(0xFF2F7A55, 0xFF3D6E7A, 0xFF7FD1A4), p(0xFF6A4FA8, 0xFF3F7F8F, 0xFFB49CF0), p(0xFF2E7468, 0xFF5A5AA0, 0xFF86D4C4), p(0xFF3F7A3A, 0xFF2F6E6A, 0xFFA6D98A)),
        "love" to listOf(p(0xFF9A3F6A, 0xFF7D4A8A, 0xFFEE9CC0), p(0xFF93405A, 0xFF8A4A74, 0xFFF0A0B4), p(0xFF8A3F78, 0xFF9A4F5E, 0xFFE2A0D8)),
        "sad" to listOf(p(0xFF3D5684, 0xFF4A5A6E, 0xFF93ABD6), p(0xFF40497E, 0xFF3A6070, 0xFFA0A8E0), p(0xFF4A5568, 0xFF3B5A8A, 0xFFA8B4C8)),
        "hype" to listOf(p(0xFFA35A24, 0xFF8A3F55, 0xFFF0B36E), p(0xFF9A4A2A, 0xFF7A4A8A, 0xFFF2A07A), p(0xFF8A6A1E, 0xFF3F7080, 0xFFE8CC7A)),
    )
    val NAMES = mapOf("neutral" to "✨ Calm", "angry" to "😡 Rage mode", "happy" to "🌈 Good vibes", "love" to "💖 Love", "sad" to "🌧 Blue", "hype" to "🔥 Hype")

    private val rx = HashMap<String, Regex>()
    private fun hit(t: String, k: String): Boolean =
        if (k[0] in 'a'..'z') rx.getOrPut(k) { Regex("(^|[^a-z])" + Regex.escape(k) + if (k.length < 5) "([^a-z]|$)" else "") }.containsMatchIn(t)
        else t.contains(k)

    fun detect(msgs: List<JSONObject>): String {
        val score = HashMap<String, Double>()
        msgs.takeLast(8).forEachIndexed { i, m ->
            val raw = m.optString("text"); val t = raw.lowercase(); val w = 1 + i / 2.0
            LEX.forEach { (mood, words) -> words.forEach { if (hit(t, it)) score[mood] = (score[mood] ?: 0.0) + w } }
            if (Regex("[A-Z]{5,}").containsMatchIn(raw) && raw.contains('!')) score["angry"] = (score["angry"] ?: 0.0) + w * 0.5
        }
        return score.filter { it.value > 0.9 }.maxByOrNull { it.value }?.key ?: "neutral"
    }
}
