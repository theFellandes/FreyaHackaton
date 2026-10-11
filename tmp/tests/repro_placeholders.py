# ruff: noqa
import json, sys, re
sys.path.insert(0, "tests/unit")
from test_untyped_products_and_text_notes import _api, _ledger, _ir
from builder.compiler.compile import compile_workflow

for sentence in ["Ödeme sözünüz {tutar} TL olarak {tarih} tarihine kaydedildi.",
                 "Your payment promise of {amount} on {date} is recorded.",
                 "Ödeme sözünüz [tutar] TL olarak <tarih> tarihine kaydedildi."]:
    ir = _ir()
    ir = ir.model_copy(update={"phrases": ir.phrases.model_copy(update={"recorded_sentence": sentence})})
    for profile in ("staged", "lean"):
        c = compile_workflow(ir, _api(), _ledger(), profile=profile)
        print("=====", profile, sentence)
        for name, node in c.workflow["nodes"].items():
            blob = json.dumps(node, ensure_ascii=False)
            if "kaydedildi" in blob or "payment promise" in blob:
                for k in ("on_enter", "task_message"):
                    v = json.dumps(node.get(k), ensure_ascii=False)
                    for m in re.finditer(r".{0,200}(kaydedildi|payment promise).{0,200}", v):
                        print(" ", name, k, "::", m.group(0))
        for n in c.notes:
            if "named" in n or "recorded" in n.lower(): print("  NOTE", n)
