import json, copy
from pathlib import Path
from builder.ingest.tool_api import parse_tool_api, with_base_url
from builder.understand.tool_roles import rename_to_roles, roles_from_document
from builder.understand.validate import tool_vocabulary, validate_policy
from builder.policy.ir import PolicyIR, RuleLedger
F = Path("tests/fixtures/invented_tr_build")
doc = json.loads((F/"openapi.json").read_text(encoding="utf-8"))
props = doc["paths"]["/hesap-ozeti"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]["properties"]["kartlar"]["items"]["properties"]
props["kart_type"] = {"type": "string", "description": "Card type as text, e.g. 'Kredi Kartı' or 'Ek Kart'."}
api = parse_tool_api(doc, every_operation=True)
api = with_base_url(rename_to_roles(api, roles_from_document(api, doc)), "https://x.example")
v = tool_vocabulary(api)
print(v.field_names.get("product_type"), v.product_types, sorted(v.product_fields))
ir = PolicyIR.model_validate_json((F/"ir.json").read_text(encoding="utf-8"))
ir = ir.model_copy(update={"disclosure": ir.disclosure.model_copy(update={"product_naming": {"kredi_karti": "sonu {son_dort} ile biten kredi kartı"}}),
     "amounts": ir.amounts.model_copy(update={"floor_field_by_type": {"kredi_karti": "asgari_tutar"}})})
for i in validate_policy(ir, RuleLedger.model_validate_json((F/"ledger.json").read_text(encoding="utf-8")), api):
    if i.severity == "error": print(i.code, i.message)
