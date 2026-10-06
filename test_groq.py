"""
Groq connectivity + model test.
Usage:   python test_groq.py gsk_your_key_here
Shows: is your key valid, which models your key can use, and a real reply from the best one.
"""
import sys, time
import groq_client as g

key = (sys.argv[1] if len(sys.argv) > 1 else input("Groq API key: ")).strip()

t = time.time()
models = g.list_models(key, force=True)
if not models:
    print(f"Could not fetch model list ({time.time()-t:.1f}s). Check the key and your internet / VPN / firewall.")
else:
    print(f"Models available to your key ({time.time()-t:.1f}s):")
    for m in models:
        print("  -", m)
    print("EduHire will use:", g.pick_model(key))

t = time.time()
text, err = g.chat([{"role": "user", "content": "Reply with the single word OK"}], key, max_tokens=20)
print(f"\nChat test: {'OK' if text else 'FAILED'} in {time.time()-t:.1f}s ->", text or g.error_message(err) or err)
