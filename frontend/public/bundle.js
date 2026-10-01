/* @ds-bundle: {"format":4,"namespace":"EdSider","components":[{"name":"PairReview"},{"name":"Verdict"},{"name":"Score"},{"name":"GuardNote"},{"name":"QuestionCard"},{"name":"DecisionBar"},{"name":"DraftCard"},{"name":"McqOptions"},{"name":"McqEditor"},{"name":"StatusPill"},{"name":"LanguagePill"},{"name":"Notice"},{"name":"Stat"},{"name":"Button"},{"name":"Field"},{"name":"Toggle"},{"name":"PageHeader"},{"name":"TopNav"},{"name":"SectionRail"}]} */
(function () {
  var React = window.React, h = React.createElement, F = React.Fragment;
  function cx() { return Array.prototype.filter.call(arguments, Boolean).join(" "); }
  function pct(x) { return Math.round(x * 100) + "%"; }

  // Outline icons, 24px grid, 1.8 stroke, currentColor - the site draws its own inline SVGs in this style.
  var ICON = {
    search: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM21 21l-4.3-4.3",
    copy: "M9 9h10v12H9zM5 15V3h10",
    paper: "M6 2h9l5 5v15H6zM14 2v6h6M9 13h8M9 17h6",
    check: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM8 12l3 3 5-6",
    upload: "M12 16V4M7 9l5-5 5 5M4 20h16",
    bank: "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zM4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
    plus: "M12 5v14M5 12h14",
    users: "M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM2 21c0-3.9 3.1-7 7-7s7 3.1 7 7M16 3.5a4 4 0 0 1 0 7.5M22 21c0-3-1.8-5.6-4.5-6.6"
  };
  function Icon(p) {
    return h("svg", { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.8, strokeLinecap: "round", strokeLinejoin: "round", "aria-hidden": true, width: p.size || 20, height: p.size || 20 }, h("path", { d: ICON[p.name] || ICON.plus }));
  }

  // Bands mirror src/dedupe/pipeline.py: AUTO_THRESHOLD 0.92, REVIEW_THRESHOLD 0.85.
  var VERDICT = {
    "LIKELY DUPLICATE": ["Likely duplicate", "dup"], "NEEDS REVIEW": ["Needs review", "review"], "DISTINCT": ["Distinct", "distinct"],
    "REPEAT ON ASSESSMENT": ["Repeat on assessment", "dup"], "EXACT MATCH": ["Exact match", "dup"],
    "DUPLICATE": ["Duplicate", "dup"], "NOT A DUPLICATE": ["Not a duplicate", "distinct"]
  };
  function band(score) { return score >= 0.92 ? "LIKELY DUPLICATE" : score >= 0.85 ? "NEEDS REVIEW" : "DISTINCT"; }

  function Button(p) {
    return h("button", { type: p.type || "button", className: cx("es-btn", "es-btn--" + (p.variant || "secondary"), p.size === "sm" && "es-btn--sm", p.className), disabled: p.disabled, onClick: p.onClick },
      p.children, p.arrow ? h("span", { className: "es-btn__arrow", "aria-hidden": true }, "→") : null);
  }

  function Verdict(p) {
    var v = VERDICT[(p.verdict || band(p.score || 0)).toUpperCase()] || [p.verdict, "neutral"];
    return h("span", { className: cx("es-pill es-verdict", "es-tone--" + v[1]) }, p.children || v[0]);
  }

  var STATUS = {
    uploaded: ["Uploaded", "neutral"], extracted: ["Extracted", "neutral"], checked: ["Ready to assign", "primary"],
    teacher_review: ["Awaiting teacher review", "review"], refer_back: ["Sent back for changes", "dup"],
    review_done: ["Review done", "ok"], published: ["Published", "ok"]
  };
  function StatusPill(p) {
    var s = STATUS[p.status] || [p.status, "neutral"];
    return h("span", { className: cx("es-pill", "es-tone--" + s[1]) }, p.children || s[0]);
  }

  var LANG = { si: "සිංහල", ta: "தமிழ்", en: "English" };
  function LanguagePill(p) {
    return h("span", { className: cx("es-pill", "es-lang--" + p.lang), lang: p.lang }, p.children || LANG[p.lang]);
  }

  function Score(p) {
    var score = p.score, raw = p.raw, tone = VERDICT[band(score)][1];
    var ticks = p.thresholds || [0.85, 0.92];
    var lo = p.min != null ? p.min : 0.7, span = 1 - lo;
    var lowered = raw != null && raw > score;
    function at(x) { return Math.max(0, Math.min(100, (x - lo) / span * 100)) + "%"; }
    return h("div", { className: "es-score", role: "img", "aria-label": pct(score) + " similar" + (lowered ? ", lowered from " + pct(raw) : "") },
      h("div", { className: "es-score__nums" },
        h("span", { className: "es-score__value" }, pct(score)),
        lowered ? h("span", { className: "es-score__raw" }, pct(raw)) : null,
        h("span", { className: "es-caption" }, "similar")),
      h("div", { className: "es-score__track" },
        lowered ? h("div", { className: "es-score__rawfill", style: { width: at(raw) } }) : null,
        h("div", { className: "es-score__fill", style: { width: at(score), background: "var(--" + tone + ")" } }),
        ticks.map(function (t) { return h("div", { key: t, className: "es-score__tick", style: { left: at(t) }, title: "threshold " + t }); })),
      p.legend === false ? null : h("div", { className: "es-score__legend" }, h("span", null, lo.toFixed(2)), ticks.map(function (t) { return h("span", { key: t }, t.toFixed(2)); }), h("span", null, "1.00")));
  }

  function GuardNote(p) {
    var reasons = (p.reasons || []).map(function (r) { return typeof r === "string" ? { text: r } : r; });
    if (!reasons.length) return null;
    return h("div", { className: "es-guard", role: "note" },
      h("div", { className: "es-guard__head" },
        h("span", null, "Safety checks fired"),
        p.raw != null ? h("span", { className: "es-guard__drop" }, "score lowered " + pct(p.raw) + " to " + pct(p.score)) : null),
      h("ul", null, reasons.map(function (r, i) {
        return h("li", { key: i }, h("span", { className: "es-guard__name" }, r.guard || "Guard"), h("span", null, r.text));
      })));
  }

  function Notice(p) {
    var tone = { info: "neutral", success: "ok", warning: "review", error: "dup", guard: "primary" }[p.tone || "info"];
    return h("div", { className: cx("es-notice", "es-tone--" + tone), role: p.tone === "error" ? "alert" : "status" },
      p.title ? h("p", { className: "es-notice__title" }, p.title) : null,
      typeof p.children === "string" ? h("p", null, p.children) : p.children);
  }

  function QuestionCard(p) {
    var meta = ["Grade " + (p.grade || "not set"), p.subject || "subject not set", p.topic || "untagged"].join(" - ");
    return h("article", { className: "es-card es-qcard" },
      h("div", { className: "es-qcard__head" },
        h("span", null, h("span", { className: "es-id" }, "#" + p.id), p.number ? h("span", { className: "es-caption" }, "  ·  question " + p.number + " on this paper") : null),
        p.lang && p.lang !== "en" ? h(LanguagePill, { lang: p.lang }) : p.badge || null),
      h("p", { className: "es-question-text", lang: p.lang }, p.text),
      h("p", { className: "es-caption", lang: p.lang }, meta, p.contributor ? " - by " + p.contributor : ""),
      p.assessments && p.assessments.length ? h("p", { className: "es-qcard__also" }, "Also appears on assessment(s): ", p.assessments.map(function (a, i) { return h(F, { key: a }, i ? ", " : "", h("b", null, "#" + a)); })) : null);
  }

  function DecisionBar(p) {
    return h("div", { className: "es-decide" },
      h(Button, { variant: "primary", onClick: p.onConfirm }, "Confirm duplicate"),
      h(Button, { variant: "secondary", onClick: p.onDistinct }, "Not a duplicate"),
      h(Button, { variant: "ghost", onClick: p.onSkip }, "Skip"),
      h("span", { className: "es-decide__spacer" }),
      h("span", { className: "es-caption" }, p.hint || "Nothing is merged or deleted. Your verdict is recorded."));
  }

  function PairReview(p) {
    var verdict = p.verdict || band(p.score);
    return h("section", { className: "es-pair" },
      h("div", { className: "es-pair__head" },
        h("div", { className: "es-row" }, h(Verdict, { verdict: verdict }), p.context ? h("span", { className: "es-caption" }, p.context) : null),
        h(Score, { score: p.score, raw: p.raw, legend: false })),
      h("div", { className: "es-pair__body" },
        p.decision ? h(Notice, { tone: "info" }, "Already reviewed: " + p.decision.verdict + ", by " + p.decision.by + " at " + p.decision.at + ". Choosing again replaces that verdict.") : null,
        p.passageNote ? h(Notice, { tone: "info", title: "Shared passage" }, p.passageNote) : null,
        h("div", { className: "es-pair__qs" }, h(QuestionCard, p.a), h(QuestionCard, p.b)),
        h(GuardNote, { reasons: p.reasons, raw: p.raw, score: p.score })),
      p.readOnly ? null : h(DecisionBar, { onConfirm: p.onConfirm, onDistinct: p.onDistinct, onSkip: p.onSkip }));
  }

  function McqOptions(p) {
    var correct = (p.correct || "").toLowerCase();
    return h("div", { className: "es-mcq" }, (p.options || []).map(function (o) {
      var ok = o.label.toLowerCase() === correct;
      return h("div", { key: o.label, className: cx("es-mcq__opt", ok && "es-mcq__opt--correct") },
        h("span", { className: "es-mcq__key" }, o.label), h("span", null, o.text), ok ? h("span", { className: "es-mcq__correct" }, "Correct") : null);
    }));
  }

  // Editable options for an extracted MCQ. Mirrors EdSider's own question editor
  // (QuestionEditorForm): one box per choice, a Correct tick, Remove, Add option,
  // and One/Many correct answers - so a promoted question lands in the shape
  // their editor already reads (stem + one block per option, answer as marks per option).
  function McqEditor(p) {
    var init = (p.options || []).map(function (o, i) { return { id: i, text: o.text }; });
    var s1 = React.useState(init), opts = s1[0], setOpts = s1[1];
    var s2 = React.useState(function () {
      var c = String(p.correct || "").toLowerCase().split(",").filter(Boolean);
      return (p.options || []).map(function (o) { return c.indexOf(String(o.label).toLowerCase()) >= 0; });
    }), correct = s2[0], setCorrect = s2[1];
    var s3 = React.useState(!!p.many), many = s3[0], setMany = s3[1];
    var s4 = React.useState(init.length), next = s4[0], setNext = s4[1];
    var group = React.useRef("mcq-" + Math.random().toString(36).slice(2, 8)).current;
    var nCorrect = correct.filter(Boolean).length;
    var problem = opts.length < 2 ? "Add at least two options."
      : opts.some(function (o) { return !o.text.trim(); }) ? "Complete every option, or remove the empty one."
      : nCorrect === 0 ? "Mark the correct answer."
      : (!many && nCorrect > 1) ? "Mark exactly one correct answer, or allow more than one." : "";
    function setText(i, v) { setOpts(opts.map(function (o, j) { return j === i ? { id: o.id, text: v } : o; })); }
    function tick(i) { setCorrect(correct.map(function (c, j) { return many ? (j === i ? !c : c) : j === i; })); }
    function remove(i) { setOpts(opts.filter(function (_, j) { return j !== i; })); setCorrect(correct.filter(function (_, j) { return j !== i; })); }
    function add() { setOpts(opts.concat([{ id: next, text: "" }])); setCorrect(correct.concat([false])); setNext(next + 1); }
    function setMode(m) { setMany(m); if (!m && nCorrect > 1) { var first = correct.indexOf(true); setCorrect(correct.map(function (_, j) { return j === first; })); } }
    return h("fieldset", { className: "es-mcqed" },
      h("div", { className: "es-mcqed__head" },
        h("legend", { className: "es-field__label" }, "Answer options"),
        h("div", { className: "es-seg", role: "radiogroup", "aria-label": "Correct answers" },
          h("button", { type: "button", role: "radio", "aria-checked": !many, onClick: function () { setMode(false); } }, "One correct"),
          h("button", { type: "button", role: "radio", "aria-checked": many, onClick: function () { setMode(true); } }, "More than one"))),
      opts.map(function (o, i) {
        var id = group + "-" + o.id;
        return h("div", { key: o.id, className: cx("es-mcqed__row", correct[i] && "es-mcqed__row--correct") },
          h("label", { className: "es-mcq__key", htmlFor: id }, i + 1),
          h("input", { id: id, className: "es-input", value: o.text, placeholder: "Option " + (i + 1), onChange: function (e) { setText(i, e.target.value); } }),
          h("label", { className: "es-check es-mcqed__correct" },
            h("input", { type: many ? "checkbox" : "radio", name: group, checked: !!correct[i], onChange: function () { tick(i); } }), "Correct"),
          h("button", { type: "button", className: "es-btn es-btn--ghost es-btn--sm", "aria-label": "Remove option " + (i + 1), onClick: function () { remove(i); } }, "Remove"));
      }),
      h("div", { className: "es-mcqed__foot" },
        h(Button, { variant: "secondary", size: "sm", onClick: add }, "Add option"),
        problem ? h("span", { className: "es-mcqed__problem", role: "status" }, problem) : h("span", { className: "es-caption" }, opts.length + " options, " + nCorrect + " correct")));
  }

  function DraftCard(p) {
    var st = React.useState(!!p.reviewed), reviewed = st[0], setReviewed = st[1];
    var isStem = p.kind === "stem";
    return h("article", { className: cx("es-card es-draft", p.depth > 0 && "es-draft--part", isStem && "es-draft--stem"), style: p.depth > 1 ? { marginLeft: 30 * p.depth } : null },
      h("div", { className: "es-draft__head" },
        h("span", { className: "es-draft__label" }, p.label),
        isStem ? h("span", { className: "es-caption" }, "Shared stem - not checked for duplicates on its own") : null,
        h("span", { style: { flex: 1 } }),
        p.marks != null ? h("span", { className: "es-caption" }, p.marks + " marks") : null,
        reviewed ? h("span", { className: "es-pill es-tone--ok" }, "Reviewed") : h("span", { className: "es-pill es-tone--neutral" }, "Not reviewed")),
      p.editable === false ? h("p", { className: "es-question-text" }, p.text) : h("textarea", { className: "es-input", defaultValue: p.text, "aria-label": p.label + " text", rows: 3 }),
      p.options ? (p.editable === false ? h(McqOptions, { options: p.options, correct: p.correct }) : h(McqEditor, { options: p.options, correct: p.correct, many: p.many })) : null,
      p.answer ? h("div", { className: "es-field" }, h("span", { className: "es-field__label" }, "Marking guidance"), h("p", { className: "es-question-text", style: { fontSize: 15, lineHeight: "24px" } }, p.answer)) : null,
      h("div", { className: "es-draft__foot" },
        h("label", { className: "es-check" }, h("input", { type: "checkbox", checked: reviewed, onChange: function (e) { setReviewed(e.target.checked); } }), "Marked as reviewed"),
        h("span", { style: { flex: 1 } }),
        p.onRemove !== false ? h(Button, { variant: "ghost", size: "sm" }, "Remove") : null));
  }

  function Stat(p) {
    return h("div", { className: "es-stat" },
      h("span", { className: cx("es-stat__icon", "es-accent--" + (p.accent || "primary")) }, h(Icon, { name: p.icon || "paper" })),
      h("span", null, h("span", { className: "es-stat__value" }, p.value), h("span", { className: "es-stat__label" }, p.label)));
  }

  function Field(p) {
    var id = p.id || "f-" + String(p.label).replace(/\W+/g, "-").toLowerCase();
    var control = p.as === "textarea" ? h("textarea", { id: id, className: "es-input", placeholder: p.placeholder, defaultValue: p.value, rows: p.rows || 4 })
      : p.as === "select" ? h("select", { id: id, className: "es-input", defaultValue: p.value }, (p.options || []).map(function (o) { return h("option", { key: o }, o); }))
      : h("input", { id: id, className: "es-input", placeholder: p.placeholder, defaultValue: p.value, type: p.type || "text" });
    return h("div", { className: "es-field" }, h("label", { className: "es-field__label", htmlFor: id }, p.label), control, p.help ? h("p", { className: "es-caption" }, p.help) : null);
  }

  function Toggle(p) {
    var st = React.useState(!!p.checked), on = st[0], set = st[1];
    return h("label", { className: "es-toggle" }, h("input", { type: "checkbox", role: "switch", checked: on, onChange: function (e) { set(e.target.checked); } }), h("span", { "aria-hidden": true }), p.label);
  }

  function PageHeader(p) {
    return h("header", { className: "es-hero" },
      h("h1", { className: "es-h", style: { fontSize: "33.6px", lineHeight: "40px", fontWeight: 800 } },
        p.accent ? h("span", { style: { color: "var(--primary)" } }, p.accent + " ") : null, p.title),
      p.subtitle ? h("p", { className: "es-hero__sub" }, p.subtitle) : null,
      p.children ? h("div", { className: "es-row", style: { marginTop: 18 } }, p.children) : null);
  }

  function TopNav(p) {
    var st = React.useState(p.active), active = st[0], set = st[1];
    return h("nav", { className: "es-nav", "aria-label": "Pages" },
      h("span", { className: "es-nav__brand" }, p.title || "EdSider"),
      h("ul", null, (p.pages || []).map(function (pg) {
        return h("li", { key: pg }, h("a", { tabIndex: 0, "aria-current": pg === active ? "page" : undefined, onClick: function () { set(pg); } }, pg));
      })),
      p.who ? h("span", { className: "es-nav__who" }, p.who) : null);
  }

  function SectionRail(p) {
    var st = React.useState(p.active || (p.items[0] && p.items[0].id)), active = st[0], set = st[1];
    return h("ul", { className: "es-rail", "aria-label": p.label || "Sections" }, (p.items || []).map(function (it) {
      return h("li", { key: it.id }, h("button", { type: "button", title: it.label, "aria-label": it.label, "aria-current": it.id === active ? "true" : undefined, onClick: function () { set(it.id); } }, h(Icon, { name: it.icon })));
    }));
  }

  window.EdSider = { Button: Button, Verdict: Verdict, StatusPill: StatusPill, LanguagePill: LanguagePill, Score: Score, GuardNote: GuardNote, Notice: Notice, QuestionCard: QuestionCard, DecisionBar: DecisionBar, PairReview: PairReview, McqOptions: McqOptions, McqEditor: McqEditor, DraftCard: DraftCard, Stat: Stat, Field: Field, Toggle: Toggle, PageHeader: PageHeader, TopNav: TopNav, SectionRail: SectionRail, Icon: Icon, band: band };
})();
