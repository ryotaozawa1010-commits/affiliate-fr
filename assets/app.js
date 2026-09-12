/* ---------------------------------------------------------------
 * 日仏 製菓用語辞典
 * data/terms.json を読み込んで、検索・絞り込み・表示を行う。
 * ビルド不要。依存ライブラリなし。
 * --------------------------------------------------------------- */
(function () {
  'use strict';

  var DATA_URL = 'data/terms.json';

  var state = {
    terms: [],
    categories: [],
    categoryById: {},
    affiliateTag: '',
    query: '',
    category: 'all'
  };

  var el = {
    search: document.getElementById('search'),
    clear: document.getElementById('clear'),
    categories: document.getElementById('categories'),
    featuredSection: document.getElementById('featured-section'),
    featuredTitle: document.getElementById('featured-title'),
    featuredList: document.getElementById('featured-list'),
    resultsTitle: document.getElementById('results-title'),
    list: document.getElementById('list'),
    count: document.getElementById('count'),
    empty: document.getElementById('empty'),
    loading: document.getElementById('loading')
  };

  /* -------------------------------------------------------------
   * 文字の正規化（フォールディング）
   *
   * 検索は「é と e」「マリーズ と まりーず」を同じものとして扱いたい。
   * そこで比較用の文字列を作るが、ハイライト表示のために
   * 「正規化後の何文字目が、元の文字列の何文字目から来たか」を
   * 対応表 (map) として一緒に持っておく。
   * 例: "coudée" → out:"coudee" / map:[0,1,2,3,4,5]
   * ----------------------------------------------------------- */

  // 合字など、1 文字が複数文字に開くもの
  var LIGATURES = { 'œ': 'oe', 'Œ': 'oe', 'æ': 'ae', 'Æ': 'ae', 'ß': 'ss' };

  // 検索時は無視する記号（中黒・長音・ハイフン類・スペース・アポストロフィ）
  var IGNORED = /[・･ー‐-―\-\s'’]/;

  function foldChar(ch) {
    if (Object.prototype.hasOwnProperty.call(LIGATURES, ch)) return LIGATURES[ch];
    if (IGNORED.test(ch)) return '';

    // 全角英数 → 半角、半角カナ → 全角カナ など
    var c = ch.normalize('NFKC');
    // アクセント記号（結合文字）を落とす: é → e
    c = c.normalize('NFD').replace(/[\u0300-\u036f]/g, '');
    // ひらがな → カタカナ（「まりーず」でも引けるように）
    c = c.replace(/[ぁ-ゖ]/g, function (h) {
      return String.fromCharCode(h.charCodeAt(0) + 0x60);
    });
    return c.toLowerCase();
  }

  function fold(str) {
    var out = '';
    var map = [];
    var chars = Array.from(str || '');
    var i, j, folded;
    for (i = 0; i < chars.length; i++) {
      folded = foldChar(chars[i]);
      for (j = 0; j < folded.length; j++) {
        out += folded[j];
        map.push(i);
      }
    }
    return { out: out, map: map, chars: chars };
  }

  /* -------------------------------------------------------------
   * 検索
   * ----------------------------------------------------------- */

  function tokenize(query) {
    return (query || '')
      .split(/[\s　]+/)
      .map(function (t) { return fold(t).out; })
      .filter(function (t) { return t.length > 0; });
  }

  function matches(term, tokens) {
    if (tokens.length === 0) return true;
    var hay = term._hay;
    return tokens.every(function (t) { return hay.indexOf(t) !== -1; });
  }

  /* -------------------------------------------------------------
   * 描画
   * ----------------------------------------------------------- */

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // 元の文字列を壊さずに、一致部分だけ <mark> で囲む
  function highlight(text, tokens) {
    if (!text) return '';
    if (!tokens.length) return escapeHtml(text);

    var f = fold(text);
    var ranges = [];

    tokens.forEach(function (token) {
      var from = 0, at;
      while ((at = f.out.indexOf(token, from)) !== -1) {
        var start = f.map[at];
        var endIdx = at + token.length;
        var end = endIdx < f.map.length ? f.map[endIdx] : f.chars.length;
        ranges.push([start, end]);
        from = at + token.length;
      }
    });

    if (!ranges.length) return escapeHtml(text);

    // 範囲をマージ（トークン同士が重なっても壊れないように）
    ranges.sort(function (a, b) { return a[0] - b[0]; });
    var merged = [ranges[0]];
    ranges.slice(1).forEach(function (r) {
      var last = merged[merged.length - 1];
      if (r[0] <= last[1]) last[1] = Math.max(last[1], r[1]);
      else merged.push(r);
    });

    var html = '';
    var cursor = 0;
    merged.forEach(function (r) {
      html += escapeHtml(f.chars.slice(cursor, r[0]).join(''));
      html += '<mark>' + escapeHtml(f.chars.slice(r[0], r[1]).join('')) + '</mark>';
      cursor = r[1];
    });
    html += escapeHtml(f.chars.slice(cursor).join(''));
    return html;
  }

  // amazon_url にアフィリエイトタグが無ければ付ける（あれば尊重する）
  function withTag(url) {
    if (!state.affiliateTag) return url;
    try {
      var u = new URL(url, location.href);
      if (!u.searchParams.has('tag')) u.searchParams.set('tag', state.affiliateTag);
      return u.toString();
    } catch (e) {
      return url;
    }
  }

  function cardHtml(term, tokens) {
    var cat = state.categoryById[term.category];
    var url = (term.amazon_url || '').trim();

    var html = '<article class="card' + (term.featured ? ' card--featured' : '') + '">';

    html += '<p class="card__fr">';
    if (term.article) html += '<span class="card__article">' + escapeHtml(term.article) + '</span>';
    html += highlight(term.fr, tokens) + '</p>';

    if (term.kana) html += '<p class="card__kana">' + highlight(term.kana, tokens) + '</p>';

    html += '<p class="card__ja">' + highlight(term.ja, tokens) + '</p>';

    if (term.note) html += '<p class="card__note">' + escapeHtml(term.note) + '</p>';

    // amazon_url が空の用語にはボタンを出さない（後から埋める運用）
    if (cat || url) {
      html += '<div class="card__foot">';
      if (cat) html += '<span class="tag">' + escapeHtml(cat.ja) + '</span>';
      if (url) {
        html += '<a class="buy" href="' + escapeHtml(withTag(url)) + '"' +
                ' target="_blank" rel="noopener sponsored"' +
                ' aria-label="' + escapeHtml(term.fr) + ' を Amazon.fr で見る">Amazon.fr で見る</a>';
      }
      html += '</div>';
    }

    html += '</article>';
    return html;
  }

  function renderCategories() {
    var buttons = [{ id: 'all', ja: 'すべて', fr: '' }].concat(state.categories);
    el.categories.innerHTML = buttons.map(function (c) {
      return '<button type="button" class="chip" data-id="' + escapeHtml(c.id) + '"' +
             ' aria-pressed="' + (state.category === c.id ? 'true' : 'false') + '">' +
             escapeHtml(c.ja) +
             (c.fr ? '<span class="chip__fr">' + escapeHtml(c.fr) + '</span>' : '') +
             '</button>';
    }).join('');
  }

  function render() {
    var tokens = tokenize(state.query);
    var isBrowsing = tokens.length === 0 && state.category === 'all';

    var results = state.terms.filter(function (t) {
      return (state.category === 'all' || t.category === state.category) && matches(t, tokens);
    });

    // 「まず揃えたい◯点」は、検索も絞り込みもしていないときだけ出す
    // （検索中は結果一覧と二重に並んでしまうため）
    var featured = state.terms.filter(function (t) { return t.featured; });
    if (isBrowsing && featured.length) {
      el.featuredTitle.textContent = 'まず揃えたい' + featured.length + '点';
      el.featuredList.innerHTML = featured.map(function (t) { return cardHtml(t, []); }).join('');
      el.featuredSection.hidden = false;
    } else {
      el.featuredSection.hidden = true;
      el.featuredList.innerHTML = '';
    }

    el.resultsTitle.textContent = isBrowsing ? 'すべての用語' : '検索結果';
    el.list.innerHTML = results.map(function (t) { return cardHtml(t, tokens); }).join('');
    el.count.textContent = results.length + ' / ' + state.terms.length + ' 語';
    el.empty.hidden = results.length > 0;
    el.clear.hidden = state.query.length === 0;

    // 絞り込み条件を URL に残す（リロード・共有しても同じ画面に戻れる）
    var params = new URLSearchParams();
    if (state.query) params.set('q', state.query);
    if (state.category !== 'all') params.set('cat', state.category);
    var qs = params.toString();
    try {
      history.replaceState(null, '', qs ? '?' + qs : location.pathname);
    } catch (e) {
      /* file:// で開いたときなどは失敗するが、表示自体には影響しない */
    }
  }

  /* -------------------------------------------------------------
   * 起動
   * ----------------------------------------------------------- */

  function setup(data) {
    state.affiliateTag = data.affiliate_tag || '';
    state.categories = Array.isArray(data.categories) ? data.categories : [];
    state.categories.forEach(function (c) { state.categoryById[c.id] = c; });

    state.terms = (Array.isArray(data.terms) ? data.terms : []).map(function (t) {
      // 検索用の干し草の山をあらかじめ作っておく（毎回の入力で作り直さない）
      t._hay = fold([t.ja, t.fr, t.kana, t.article ? t.article + ' ' + t.fr : '', t.note]
        .filter(Boolean).join(' ｜ ')).out;
      return t;
    });

    var params = new URLSearchParams(location.search);
    state.query = params.get('q') || '';
    var cat = params.get('cat');
    if (cat && state.categoryById[cat]) state.category = cat;
    el.search.value = state.query;

    el.loading.hidden = true;
    renderCategories();
    render();

    el.search.addEventListener('input', function () {
      state.query = el.search.value;
      render();
    });

    el.clear.addEventListener('click', function () {
      state.query = '';
      el.search.value = '';
      el.search.focus();
      render();
    });

    el.categories.addEventListener('click', function (e) {
      var chip = e.target.closest('.chip');
      if (!chip) return;
      state.category = chip.dataset.id;
      Array.prototype.forEach.call(el.categories.children, function (c) {
        c.setAttribute('aria-pressed', c === chip ? 'true' : 'false');
      });
      render();
    });
  }

  fetch(DATA_URL, { cache: 'no-cache' })
    .then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.json();
    })
    .then(setup)
    .catch(function (err) {
      el.loading.innerHTML =
        '用語データ (data/terms.json) を読み込めませんでした。<br>' +
        '<span class="empty__hint">ローカルで確認するときは、ファイルを直接開かず ' +
        '<code>python3 -m http.server</code> などで配信してください。</span>';
      console.error(err);
    });
})();
