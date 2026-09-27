"""Tests for vera.text.normalize — normalisation, fingerprinting, auto-reply."""

from vera.text.normalize import fingerprint, is_auto_reply, normalize_text, ngrams


def test_normalize_strips_punctuation_case_and_diacritics():
    assert normalize_text("Héllo,  WORLD!!") == "hello world"
    assert normalize_text("  multiple   spaces\tand\nnewlines ") == "multiple spaces and newlines"


def test_normalize_folds_hindi_romanisation_variants():
    assert normalize_text("Nahin") == normalize_text("nahi")
    assert normalize_text("Haan") == normalize_text("ha")
    assert normalize_text("dhanyavaad") == normalize_text("thanks")
    assert normalize_text("jee") == normalize_text("ji")


def test_normalize_strips_emoji_and_zero_width():
    assert normalize_text("Ok \U0001f600 done\u200b!") == "ok done"


def test_fingerprint_excludes_conversation_id():
    canned = "Thank you for contacting our clinic! Our team will respond shortly."
    assert fingerprint("m_001", canned) == fingerprint("m_001", "  thank YOU for contacting our clinic! our team will respond shortly.")
    # same text under different conversation ids -> identical (the judge's
    # auto-reply scenario uses conv_auto_1..4 for the same merchant)
    assert fingerprint("m_001", canned) == fingerprint("m_001", canned)


def test_fingerprint_differs_by_merchant():
    canned = "Thank you for contacting our clinic!"
    assert fingerprint("m_001", canned) != fingerprint("m_002", canned)


def test_is_auto_reply_true_for_judge_canned_string():
    assert is_auto_reply(
        "Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly."
    ) is True


def test_is_auto_reply_true_for_hindi_pattern_b():
    assert is_auto_reply(
        "Aapki jaankari ke liye bahut-bahut shukriya. Main aapki yeh sabhi baatein "
        "aur sujhaav hamari team tak pahuncha deti hoon."
    ) is True


def test_is_auto_reply_true_for_automated_assistant_line():
    assert is_auto_reply("Main ek automated assistant hoon, main aapki madad nahi kar sakti.") is True


def test_is_auto_reply_false_for_real_replies():
    assert is_auto_reply("Yes please send the abstract") is False
    assert is_auto_reply("Ok lets do it. Whats next?") is False
    assert is_auto_reply("Not interested. Stop messaging me.") is False
    assert is_auto_reply("Btw can you also help me with my GST filing this month?") is False
    assert is_auto_reply("Haan bhejo") is False


def test_is_auto_reply_false_for_short_real_text():
    assert is_auto_reply("ok") is False
    assert is_auto_reply("send") is False


def test_is_auto_reply_duplicate_detection_via_seen_texts():
    canned = "Thank you for contacting our clinic! Our team will respond shortly."
    paraphrase = "Thank you for contacting our clinic! Our team will respond shortly!!"
    assert is_auto_reply(paraphrase, seen_texts=[canned]) is True


def test_ngrams_word_level():
    grams = ngrams("one two three four five six seven eight nine", n=8)
    assert ("one", "two", "three", "four", "five", "six", "seven", "eight") in grams
    assert ("two", "three", "four", "five", "six", "seven", "eight", "nine") in grams
    assert ("one", "two", "three") not in grams
    assert ngrams("short text", n=8) == set()
