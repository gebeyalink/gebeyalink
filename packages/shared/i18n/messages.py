"""Every sentence the platform speaks, in every language it speaks.

Keys are grouped by what the reader is being told: what went wrong, what a field should
look like, and what arrives by text message. Values never interpolate caller-supplied
text directly — callers format with named placeholders so a missing value shows up as an
obvious placeholder rather than a broken sentence.

A key absent from a catalogue falls back to English, then to the key itself, so a missing
translation degrades to something readable instead of raising inside a request.
"""

from __future__ import annotations

from typing import Final

from packages.shared.i18n.languages import DEFAULT_LANGUAGE, Language

MESSAGES: Final[dict[Language, dict[str, str]]] = {
    Language.AM: {
        # What went wrong
        "internal_error": "የስርቅ ስህተት ተከስቷል። እባክዎ ኋላ ይሞክሩ።",
        "validation_error": "የገቢው መረጃ ትክክል አይደለም።",
        "auth_required": "በመግባት ያስፈልጋል።",
        "forbidden": "ይህን እርምጃ መውረስ አይቻልም።",
        "not_found": "የተፈለግው ነገር አልተገኘም።",
        "rate_limited": "በጣም ብዙ ሙከራ ተላኍ። እባክዎ ትንሽ ይጠብቁ።",
        "otp_locked": "የማረጋገጫ ኮድ ተዘግቷል። አዲስ ኮድ ይጠይቁ።",
        "otp_expired": "የማረጋገጫ ኮድ ትክክል አይደለም ወይም ጊዜው አልፏል።",
        "token_invalid": "የመግቢያ ምልክት ሰርቷል። እባክዎ እንደገና ይግቡ።",
        "concurrent_modification": "ሌላ ለውጥ ተደርጓል። እባክዎ እንደገና ያስገቡ።",
        "illegal_state_transition": "ይህ ለውጥ በዚህ ሁኔታ የሚጠበቅ አይደለም።",
        "offer_limit": "የቅንብር ገደብ ደርሷል።",
        "offer_conflict": "ሌላ ቅንብር ተከልቷል።",
        "order_conflict": "ከተቀረጠው ቅንብር ጋር ስለሚገጠኝ በአንድ አቅም ማስተካከል አይቻልም።",
        "offer_expired": "ቅንብሩ ጊዜው አልፏል።",
        "limit_exceeded": "የተለያየው ገደብ አልፏል።",
        "insufficient_kyc": "የማረጋገጫ ደረጃዎ አበቃዊ አይደለም።",
        "payment_failed": "ክፍያቱ አልተሳካም።",
        "payment_expired": "የክፍያት ጊዜ አልፏል።",
        "dispute_exists": "በዚህ ላይ የተከለለ ቅሬታ አለ።",
        "kyc_pending": "የመረጃ ማረጋገጫዎ በመጠበቅ ላይ ነው።",
        "listing_not_active": "ይህ ማስታወቂያ አልተነቀረም።",
        "gateway_signature_invalid": "የመልዕክት ማረጋገጫ አልተሳካም።",
        "payment_attempts_exceeded": "የክፍያት ሙከራ ጊዜዎች አልፏል።",
        "price_outlier_info": "ዋጋው ከየድር ገጽታው የተለየ ነው።",
        # Field-level guidance
        "invalid_phone": "የኢትዮጵያ ስልክ ቁጥር በትክክል ቅርጽ አይደለም።",
        "invalid_code": "የማረጋገጫ ኮድ ድል አይደለም።",
        "invalid_language": "የቋንቋ ምርጫ ትክክል አይደለም።",
        "terms_version_required": "የግንዛቤ ስምምና ደንብ ስምጣት ያስፈልጋል።",
        # Text messages
        "otp_message": "የGebeyaLink የማረጋገጫ ኮድዎ {code} ነው። በ{purpose} ላይ ይጠቀሙ። ይህን ኮድ ማናበትን አታሳል።",
        "otp_message_purpose_sign_in": "መግቢት",
        "otp_message_purpose_terms": "የግንዛቤ ስምምናና ደንብ",
    },
    Language.OM: {
        "internal_error": "Dogoggora guddaa uumame. Qophii.",
        "validation_error": "Xivaa hin dandeessine.",
        "auth_required": "Seeraan seenuu qaba.",
        "forbidden": "Kanaan raawvuu hin danda'amu.",
        "not_found": "Wanted tokko hin argamne.",
        "rate_limited": "Testoota baayyaa yoo caajeffate, mallattoo fiijaa.",
        "otp_locked": "Kooddii qabsiyyichaa lafe. Kooddii haaraa qabsi.",
        "otp_expired": "Kooddii qabsiyyichaa qofa hin taane yookaan yeroo isa darbee.",
        "token_invalid": "Ragaa seenumichaa karaa. Dubbicha irra deebi'i.",
        "concurrent_modification": "Jijjiirama biraa hojjetama. Ammaa irra deebi'i.",
        "illegal_state_transition": "Jijjiiramaan kanaa kun itti fufu hin qabu.",
        "offer_limit": "Yoojji kootuun darbee.",
        "offer_conflict": "Yoojji biraa hojjetama.",
        "order_conflict": "Qabiyyee keenyaa biraa kan qabuuf jiruun qofa mirgaa hin qabu.",
        "offer_expired": "Yoojji yeroo isa darbee.",
        "limit_exceeded": "Kootuu itti fufu yoo darree gaafame.",
        "insufficient_kyc": "Sadarkaalee ragaa keessaa hin ga'u.",
        "payment_failed": "Kaffaltii kufe.",
        "payment_expired": "Yeroo kaffaltii darbee.",
        "dispute_exists": "Saan kaanis ragaa qabu.",
        "kyc_pending": "Ragaa keessaa eegama.",
        "listing_not_active": "Kanne hin qabnee.",
        "gateway_signature_invalid": "Sirri shageeffamaa mallattoo hin taane.",
        "payment_attempts_exceeded": "Testoota kaffaltii yoo darbee gaafame.",
        "price_outlier_info": "Kaffaltii kun biraa kamiinuu kalaa.",
        "invalid_phone": "Lakkii bilisaa Itoophiyaa qofa hin taane.",
        "invalid_code": "Kooddii qabsiyyichaa dogoggora.",
        "invalid_language": "Afaan filadhu dogoggora.",
        "terms_version_required": "Waa'ee maqaa qaarmaa waliin galmaa'i qaba.",
        "otp_message": (
            "Kooddii qabsiyyichaa GebeyaLink {code} dha. {purpose} fayyadamtu itti fayyadi. "
            "Kooddii kana qof hin kennin."
        ),
        "otp_message_purpose_sign_in": "seenumuu",
        "otp_message_purpose_terms": "maqaa qaarmaa",
    },
    Language.EN: {
        "internal_error": "Something went wrong on our side. Please try again.",
        "validation_error": "The submitted details are not valid.",
        "auth_required": "You need to sign in.",
        "forbidden": "This action is not allowed.",
        "not_found": "The requested item was not found.",
        "rate_limited": "Too many attempts were sent. Please wait a moment.",
        "otp_locked": "This verification code is locked. Request a new code.",
        "otp_expired": "The verification code is wrong or has expired.",
        "token_invalid": "Your sign-in token is no longer valid. Please sign in again.",
        "concurrent_modification": "Another change was made at the same time. Please try again.",
        "illegal_state_transition": "This change is not allowed from the current state.",
        "offer_limit": "The offer limit was reached.",
        "offer_conflict": "Another offer was placed first.",
        "order_conflict": "This listing already has an order holding that weight.",
        "offer_expired": "The offer has expired.",
        "limit_exceeded": "A configured limit was exceeded.",
        "insufficient_kyc": "Your verification level is not high enough.",
        "payment_failed": "The payment did not go through.",
        "payment_expired": "The payment window has expired.",
        "dispute_exists": "There is already a dispute on this.",
        "kyc_pending": "Your verification is still being reviewed.",
        "listing_not_active": "This listing is not published.",
        "gateway_signature_invalid": "The message signature did not verify.",
        "payment_attempts_exceeded": "The payment attempt limit was reached.",
        "price_outlier_info": "This price differs from the usual range.",
        "invalid_phone": "That is not a valid Ethiopian phone number.",
        "invalid_code": "The verification code is wrong.",
        "invalid_language": "That language is not supported.",
        "terms_version_required": "Accepting the terms and privacy policy is required.",
        "otp_message": (
            "Your GebeyaLink verification code is {code}. Use it for {purpose}. "
            "Never share this code."
        ),
        "otp_message_purpose_sign_in": "signing in",
        "otp_message_purpose_terms": "accepting the terms and privacy policy",
    },
}


def translate(key: str, language: Language | None = None, **values: object) -> str:
    """Look up a sentence in the reader's language and fill its placeholders."""
    active = language or DEFAULT_LANGUAGE
    template = MESSAGES.get(active, {}).get(key) or MESSAGES[Language.EN].get(key, key)
    return template.format(**values) if values else template
