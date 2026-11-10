// Mood-reactive chat theme: scores the last few messages (keywords + emoji, newest weighted most)
// and animates the chat between palettes for that mood — e.g. angry → demon / frost / inferno red,
// happy → neon green / purple, love → rose, sad → midnight blue. Each mood cycles its palettes.
const PbMood = (() => {
  const LEX = {
    angry: ['angry', 'mad', 'hate', 'furious', 'annoyed', 'wtf', 'stupid', 'idiot', 'damn', 'shut up', 'useless',
            'pissed', 'rage', 'worst', 'irritat', 'gussa', 'bakwas', 'pagal', 'bekar', '😡', '🤬', '😠', '👿', '💢', '😤'],
    happy: ['happy', 'great', 'awesome', 'nice', 'cool', 'good', 'thanks', 'thank you', 'yay', 'lol', 'haha', 'done',
            'perfect', 'amazing', 'excellent', 'mast', 'badhiya', 'shandar', '😂', '😄', '😁', '😊', '👍', '🎉', '✅', '🤣'],
    love:  ['love', 'miss you', 'cute', 'sweet', 'dear', 'babe', 'pyaar', '❤', '😍', '🥰', '😘', '💕', '💖', '💜'],
    sad:   ['sad', 'sorry', 'cry', 'upset', 'tired', 'alone', 'hurt', 'bad day', 'miss', 'unfortunately', 'dukh',
            '😢', '😭', '😞', '😔', '💔', '🥺'],
    hype:  ['lets go', "let's go", 'fire', 'insane', 'crazy', 'epic', 'boost', 'win', 'won', 'rank', 'legend', 'beast',
            '🔥', '🚀', '⚡', '💥', '🏆', '💯', '!!!'],
  };
  // [own-bubble a, own-bubble b, accent] — muted, low-glare tones (white text on a/b stays ≥ 4.5:1);
  // the background, panels and the other side's bubbles are all tinted from the accent in pb.css.
  const PAL = {
    neutral: [['#4c4f9e', '#3d5f9e', '#8f94e0'], ['#46588f', '#3c6a80', '#86a8d8'], ['#5a4b92', '#4a578f', '#a399dc']],
    angry:   [['#8e2f36', '#7a3b2e', '#e0787a'], ['#8a3346', '#6b3a55', '#e58a9b'], ['#7a2a2a', '#8a4428', '#e08a6a'], ['#6e2228', '#5e2a3e', '#d46a72']],
    happy:   [['#2f7a55', '#3d6e7a', '#7fd1a4'], ['#6a4fa8', '#3f7f8f', '#b49cf0'], ['#2e7468', '#5a5aa0', '#86d4c4'], ['#3f7a3a', '#2f6e6a', '#a6d98a']],
    love:    [['#9a3f6a', '#7d4a8a', '#ee9cc0'], ['#93405a', '#8a4a74', '#f0a0b4'], ['#8a3f78', '#9a4f5e', '#e2a0d8']],
    sad:     [['#3d5684', '#4a5a6e', '#93abd6'], ['#40497e', '#3a6070', '#a0a8e0'], ['#4a5568', '#3b5a8a', '#a8b4c8']],
    hype:    [['#a35a24', '#8a3f55', '#f0b36e'], ['#9a4a2a', '#7a4a8a', '#f2a07a'], ['#8a6a1e', '#3f7080', '#e8cc7a']],
  };
  const NAMES = { neutral: '✨ Calm', angry: '😡 Rage mode', happy: '🌈 Good vibes', love: '💖 Love', sad: '🌧 Blue', hype: '🔥 Hype' };

  const RX = {};
  // words match at a word start ("mad" not inside "made"); emoji / punctuation match anywhere
  const hit = (t, k) => /^[a-z]/.test(k) ? (RX[k] ||= new RegExp('(^|[^a-z])' + k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + (k.length < 5 ? '([^a-z]|$)' : ''))).test(t) : t.includes(k);
  function detect(msgs) {
    const score = {}; const recent = (msgs || []).slice(-8);
    recent.forEach((m, i) => {
      const t = ` ${(m.text || '').toLowerCase()} `; const w = 1 + i / 2;   // newest counts most
      for (const [mood, words] of Object.entries(LEX))
        for (const k of words) if (hit(t, k)) score[mood] = (score[mood] || 0) + w;
      if (/[A-Z]{5,}/.test(m.text || '') && /!/.test(m.text || '')) score.angry = (score.angry || 0) + w * 0.5;  // SHOUTING!
    });
    let best = 'neutral', top = 0.9;
    for (const [mood, v] of Object.entries(score)) if (v > top) { best = mood; top = v; }
    return best;
  }

  let mood = null, idx = 0, timer = null, el = null;
  function paint() {
    const p = PAL[mood][idx % PAL[mood].length];
    el.style.setProperty('--mood-a', p[0]);
    el.style.setProperty('--mood-b', p[1]);
    el.style.setProperty('--mood-accent', p[2]);
  }
  function apply(root, msgs) {
    el = root; const next = detect(msgs);
    const tag = root.querySelector('#moodTag'); if (tag) tag.textContent = NAMES[next];   // header is re-rendered per chat
    if (next === mood) return;
    mood = next; idx = 0;
    root.dataset.mood = mood; paint();
    clearInterval(timer);
    timer = setInterval(() => { idx++; paint(); }, 15000);   // drift slowly within the mood
  }
  return { apply, detect };
})();
