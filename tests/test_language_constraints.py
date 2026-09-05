import pytest
from services.voice_gateway.tts import detect_language
from services.agent_core.agent import agent

def test_detect_language_multilingual():
    # Test English text detection
    lang_en, _ = detect_language("Pay 500 rupees to Ramesh")
    assert lang_en == "en"
    
    # Test Hindi script text detection
    lang_hi, _ = detect_language("रमेश को 500 रुपये भेजो")
    assert lang_hi == "hi"

    # Test Tamil script text detection
    lang_ta, _ = detect_language("ரமேஷிற்கு 500 ரூபாய் அனுப்பு")
    assert lang_ta == "ta"

    # Test Spanish text detection
    lang_es, _ = detect_language("Pagar 500 pesos a Ramesh por favor")
    assert lang_es == "es"

def test_agent_system_prompt_multilingual_directive():
    prompts = getattr(agent, "_system_prompts", ())
    prompt_str = " ".join([str(p) for p in prompts])
    assert "MULTILINGUAL" in prompt_str or "multilingual" in prompt_str
