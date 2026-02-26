import asyncio
import sys
from main import get_tts_model

async def test():
    print("Testing 0.6B with CustomVoice...")
    try:
        model = await get_tts_model(size="0.6B", model_type="CustomVoice")
        wavs, sr = model.generate_custom_voice(
            text="This should work without probability tensor errors.",
            language="English",
            speaker="Vivian",
            temperature=0.3,
            repetition_penalty=1.1,
            top_p=0.8,
            subtalker_temperature=0.3
        )
        print("0.6B Generation successful, wav shape:", wavs[0].shape)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Error during generation: {e}")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(test())
