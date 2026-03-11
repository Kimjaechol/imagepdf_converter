"""OCR confusion character groups and context-based correction instructions.

This module defines:
1. Confusion character groups (characters OCR engines frequently confuse)
2. Hanja confusion groups (Chinese characters commonly used in Korean documents)
3. LLM prompt instructions for context-aware OCR correction

All LLM-facing prompts (correction.py, unified_vision.py, upstage_gemini_refiner.py,
gemini_html_refiner.py) import the shared instruction text from here to ensure
consistency across the pipeline.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 1. Latin / Numeral / Symbol confusion groups
# ---------------------------------------------------------------------------
LATIN_NUMERAL_CONFUSION_GROUPS: list[dict] = [
    {"group": "0 ↔ O ↔ o", "members": ["0", "O", "o"],
     "hint": "금액/날짜 맥락이면 숫자 0, 영단어면 영문 O"},
    {"group": "1 ↔ l ↔ I ↔ | ↔ ㅣ", "members": ["1", "l", "I", "|", "ㅣ"],
     "hint": "숫자 맥락이면 1, 영단어/약어이면 I 또는 l, 로마자 번호이면 Ⅰ"},
    {"group": "rn ↔ m", "members": ["rn", "m"],
     "hint": "영단어 사전 매칭으로 판별"},
    {"group": "cl ↔ d", "members": ["cl", "d"],
     "hint": "영단어 사전 매칭으로 판별"},
    {"group": "vv ↔ w", "members": ["vv", "w"],
     "hint": "영단어 사전 매칭으로 판별"},
    {"group": "5 ↔ S ↔ s", "members": ["5", "S", "s"],
     "hint": "숫자 맥락이면 5, 영단어이면 S"},
    {"group": "8 ↔ B", "members": ["8", "B"],
     "hint": "숫자 맥락이면 8, 영단어이면 B"},
    {"group": "2 ↔ Z ↔ z", "members": ["2", "Z", "z"],
     "hint": "숫자 맥락이면 2, 갑을병정 계약서에서는 乙(을) 가능"},
    {"group": "6 ↔ G ↔ b", "members": ["6", "G", "b"],
     "hint": "숫자 맥락이면 6, 영단어이면 G 또는 b"},
    {"group": "9 ↔ g ↔ q", "members": ["9", "g", "q"],
     "hint": "숫자 맥락이면 9, 영단어이면 g 또는 q"},
]

# ---------------------------------------------------------------------------
# 2. Korean Jamo / Symbol confusion groups
# ---------------------------------------------------------------------------
KOREAN_SYMBOL_CONFUSION_GROUPS: list[dict] = [
    {"group": "ㅁ ↔ □", "members": ["ㅁ", "□"],
     "hint": "한글 자모이면 ㅁ, 도형/체크박스이면 □"},
    {"group": "ㅇ ↔ O ↔ 0", "members": ["ㅇ", "O", "0"],
     "hint": "한글 자모이면 ㅇ, 영문이면 O, 숫자이면 0"},
    {"group": "— ↔ - ↔ ㅡ ↔ 一", "members": ["—", "-", "ㅡ", "一"],
     "hint": "문장 내 구두점이면 —(em dash), 범위이면 -(hyphen), 한글 자모이면 ㅡ, 한자이면 一"},
    {"group": ", ↔ . (저해상도)", "members": [",", "."],
     "hint": "금액 천 단위 구분이면 쉼표, 소수점이면 점"},
    {"group": ": ↔ ; (저해상도)", "members": [":", ";"],
     "hint": "시간(10:30)이면 콜론, 문장구분이면 세미콜론"},
    {"group": "\" \" ↔ '' (따옴표류)", "members": ['"', '"', '"', "'", "'", "'"],
     "hint": "문맥에 따라 적절한 따옴표 사용"},
]

# ---------------------------------------------------------------------------
# 3. Hanja confusion groups (Korean documents)
# ---------------------------------------------------------------------------
HANJA_CONFUSION_GROUPS: list[dict] = [
    # Cheongan (천간 天干)
    {"hanja": "乙(을)", "confused_with": ["Z", "z", "2", "己(기)"],
     "context": "계약서 갑/을/병 체계, 등급 체계"},
    {"hanja": "丁(정)", "confused_with": ["T", "7"],
     "context": "등급 갑/을/병/정, 면허 종류에서는 숫자 가능"},
    {"hanja": "己(기)", "confused_with": ["已(이)", "巳(사)", "乙(을)"],
     "context": "천간 戊己庚 순서"},
    {"hanja": "甲(갑)", "confused_with": ["田(전)", "由(유)"],
     "context": "계약서, 등급, 시험"},
    {"hanja": "壬(임)", "confused_with": ["王(왕)", "士(사)", "任(임)"],
     "context": "천간"},
    {"hanja": "丙(병)", "confused_with": ["内(내)", "两"],
     "context": "등급, 계약서"},
    {"hanja": "庚(경)", "confused_with": ["庫(고) 일부"],
     "context": "천간"},
    {"hanja": "辛(신)", "confused_with": ["幸(행)", "立(립) 상단"],
     "context": "천간"},
    # Common Hanja in Korean documents
    {"hanja": "日(일)", "confused_with": ["目(목)", "曰(왈)", "□"],
     "context": "날짜, 요일"},
    {"hanja": "月(월)", "confused_with": ["肉(육)의 변", "円"],
     "context": "날짜"},
    {"hanja": "一(일)", "confused_with": ["ー(장음)", "-(하이픈)", "ㅡ", "—"],
     "context": "숫자, 서열"},
    {"hanja": "二(이)", "confused_with": ["=(등호)", "ニ(가타카나)"],
     "context": "숫자"},
    {"hanja": "三(삼)", "confused_with": ["≡", "彡", "ミ(가타카나)"],
     "context": "숫자"},
    {"hanja": "十(십)", "confused_with": ["+(플러스)", "†(단검표)"],
     "context": "숫자"},
    {"hanja": "人(인)", "confused_with": ["入(입)", "ス(가타카나)"],
     "context": "인명, 인원"},
    {"hanja": "大(대)", "confused_with": ["太(태)", "犬(견)"],
     "context": "크기, 등급"},
    {"hanja": "土(토)", "confused_with": ["士(사)", "±"],
     "context": "요일, 지명"},
]

# ---------------------------------------------------------------------------
# 4. Roman numeral confusion (CRITICAL)
# ---------------------------------------------------------------------------
ROMAN_NUMERAL_NOTE = """
**로마자(로마 숫자) 표기 혼동 주의 (CRITICAL)**:
로마자 번호(Ⅰ, Ⅱ, Ⅲ, Ⅳ, Ⅴ 등)는 OCR에서 가장 혼동이 심한 문자입니다.
- Ⅰ ↔ I(영문 대문자) ↔ l(영문 소문자 엘) ↔ 1(숫자) ↔ |(파이프) ↔ ㅣ(한글 자모)
- Ⅱ ↔ II ↔ ll ↔ 11
- Ⅲ ↔ III ↔ lll ↔ 111
- Ⅳ ↔ IV ↔ lV ↔ 1V
- Ⅴ ↔ V ↔ v ↔ 5

**로마자 번호 판별 규칙**:
1. 로마자 번호 다음에는 소제목이 나오는 경우가 많음 (예: "Ⅰ. 서론", "Ⅱ. 본론")
2. "제Ⅰ장", "제Ⅱ편" 등 한국어 법률/학술 문서의 편/장/절 번호로 사용
3. 목차나 개조식 번호 체계에서 사용 (Ⅰ→Ⅱ→Ⅲ 순서)
4. 단독으로 "I"이 나오면 영문 대명사일 수 있으나, "I." 또는 "I " + 한글 제목이면 로마자 Ⅰ
5. "1" 다음에 소제목이 나오면 아라비아 숫자가 아닌 로마자 Ⅰ일 가능성 확인
"""

# ---------------------------------------------------------------------------
# 5. Full LLM instruction text (embedded in prompts)
# ---------------------------------------------------------------------------

def build_ocr_confusion_instruction(include_examples: bool = True) -> str:
    """Build the OCR confusion character correction instruction for LLM prompts.

    Args:
        include_examples: Whether to include detailed context inference examples.

    Returns:
        Instruction text to embed in LLM prompts.
    """
    lines = [
        "",
        "## OCR 혼동 문자 교정 지침 (OCR Confusion Character Resolution)",
        "",
        "OCR 엔진은 시각적으로 유사한 문자를 혼동합니다. 반드시 **문맥(단어→문장→문서 전체)**을",
        "기준으로 올바른 문자를 확정하세요. 글자 단위가 아닌 문맥 단위로 판단합니다.",
        "",
        "### 1단계: 혼동 문자 쌍 인식",
        "",
        "아래 그룹의 문자들은 OCR에서 서로 혼동됩니다:",
        "",
        "**숫자 ↔ 영문자 혼동:**",
    ]

    for g in LATIN_NUMERAL_CONFUSION_GROUPS:
        lines.append(f"- {g['group']} → {g['hint']}")

    lines.extend([
        "",
        "**한글 자모 ↔ 기호 혼동:**",
    ])
    for g in KOREAN_SYMBOL_CONFUSION_GROUPS:
        lines.append(f"- {g['group']} → {g['hint']}")

    lines.extend([
        "",
        "**한자(漢字) 혼동 (한국 문서에서 중요):**",
    ])
    for g in HANJA_CONFUSION_GROUPS:
        confused = ", ".join(g["confused_with"])
        lines.append(f"- {g['hanja']} ↔ {confused} (맥락: {g['context']})")

    lines.extend([
        "",
        ROMAN_NUMERAL_NOTE,
    ])

    if include_examples:
        lines.extend([
            "",
            "### 2단계: 문맥 추론으로 확정 (Context Inference Examples)",
            "",
            '**예시 1 – 금액 맥락:**',
            '  OCR: "총 비용은 1O0,O00원입니다"',
            '  → "원"이 붙으므로 금액 → O는 숫자 0',
            '  → 확정: "총 비용은 100,000원입니다"',
            "",
            '**예시 2 – 영문 지명:**',
            '  OCR: "SEOU1 T0WER"',
            '  → 영어 단어 맥락 → 1→L, 0→O',
            '  → 확정: "SEOUL TOWER"',
            "",
            '**예시 3 – 날짜/ID:**',
            '  OCR: "접수lD: 2O23-O5I2"',
            '  → "접수ID"(l→I), 날짜 맥락에서 O→0, I→1',
            '  → 확정: "접수ID: 2023-0512"',
            "",
            '**예시 4 – 계약서 한자:**',
            '  OCR: "제2조(Z방의 의무)"',
            '  → 계약서 갑/을/병 체계 → Z는 乙(을)',
            '  → 확정: "제2조(乙방의 의무)" 또는 "제2조(을방의 의무)"',
            "",
            '**예시 5 – 등급 한자:**',
            '  OCR: "T등급 판정을 받은 경우"',
            '  → 앞뒤 문맥에 갑/을/병 등급 → T는 丁(정)',
            '  → 확정: "丁등급 판정을 받은 경우"',
            "",
            '**예시 6 – 숫자 맥락 (같은 T라도 다른 결과):**',
            '  OCR: "T종 보통면허"',
            '  → 운전면허 맥락 → 1종/2종 체계 → T는 숫자 1',
            '  → 확정: "1종 보통면허"',
            "",
            '**예시 7 – 로마자 번호:**',
            '  OCR: "1. 서론" (문서 최상위 제목)',
            '  → 다음 제목이 "ll. 본론"이면 로마자 번호 체계',
            '  → 확정: "Ⅰ. 서론", "Ⅱ. 본론"',
            "",
            '**예시 8 – 로마자 vs 숫자:**',
            '  OCR: "제III조" vs "제3조"',
            '  → 법률 문서에서 조항 번호는 아라비아 숫자 사용이 일반적',
            '  → "III"이 등장하면 문서 전체의 번호 체계 확인 필요',
            "",
            "### 핵심 원칙",
            "- **같은 글자라도 문맥에 따라 교정 결과가 다릅니다** (T → 丁 또는 T → 1)",
            "- **문서 전체의 번호 체계를 먼저 파악**하세요 (아라비아 숫자 vs 로마자 vs 한자)",
            "- **인접 문자열과 함께 판단**하세요 (단어, 조사, 단위 등)",
            "- **도메인 맥락 활용**: 계약서=갑을병정, 시험=정답/오답, 법률=조항/판결",
            "- 확신이 없으면 원본 그대로 유지 (잘못된 교정보다 미교정이 낫습니다)",
        ])

    return "\n".join(lines)


# Compact version for prompts with token budget constraints
def build_ocr_confusion_instruction_compact() -> str:
    """Build a shorter version of OCR confusion instructions for token-constrained prompts."""
    return """
## OCR Confusion Character Resolution (CRITICAL)
Fix characters that OCR engines confuse based on CONTEXT (word→sentence→document), not character-level.

**Key confusion groups:**
- 0↔O↔o, 1↔l↔I↔|↔ㅣ, 5↔S, 8↔B, 2↔Z↔z, 6↔G↔b, 9↔g↔q
- rn↔m, cl↔d, vv↔w (English words)
- ㅁ↔□, ㅇ↔O↔0, —↔-↔ㅡ↔一, ,↔. (low resolution)
- Hanja: 乙↔Z↔z↔2↔己, 丁↔T↔7, 甲↔田↔由, 壬↔王↔士, 丙↔内
- 日↔目↔曰, 大↔太↔犬, 人↔入, 土↔士, 十↔+↔†
- 一↔ー↔-↔ㅡ, 二↔=↔ニ, 三↔≡↔ミ

**CRITICAL – Roman numerals (로마자 번호):**
- Ⅰ↔I↔l↔1↔|, Ⅱ↔II↔ll↔11, Ⅲ↔III↔111, Ⅳ↔IV↔1V, Ⅴ↔V↔v↔5
- Roman numeral followed by subtitle/heading = likely Ⅰ,Ⅱ,Ⅲ (not 1,l,I)
- Check document numbering system: Arabic (1,2,3) vs Roman (Ⅰ,Ⅱ,Ⅲ) vs Hanja (甲,乙,丙)

**Context resolution rules:**
- Amount+원 → digits (1O0,O00원 → 100,000원)
- English words → letters (SEOU1 → SEOUL, T0WER → TOWER)
- Contract 갑/을/병 → Hanja (Z방 → 乙方)
- Grade system → match document convention (T등급 → 丁등급 or 1등급)
- License type → numbers (T종 면허 → 1종 면허)
- Same character can correct differently depending on context (T → 丁 or T → 1)
- When uncertain, keep original (no-correction > wrong-correction)
"""
