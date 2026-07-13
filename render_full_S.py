"""Assemble the complete `const S = {...}` block for index.html from output_2026-06.json,
keeping the parts that don't change month-to-month (revenue goals, manuals, jayJobs, role state)
and replacing the parts that do (emps, flags, spiffDetail, commLeads, carryForward, officeMems, bonuses).
"""
import json

with open("output_2026-06.json") as f:
    r = json.load(f)


def js(s):
    return json.dumps(s or "", ensure_ascii=False)


def emp_line(e):
    return (f"{{name:{js(e['name'])}, svc:{e['svc']:g}, ins:{e['ins']:g}, plb:{e['plb']:g}, "
            f"com:{e['com']:g}, chs:{e['chs']:g}, chi:{e['chi']:g}, dups:[]}}")


def flag_line(f):
    return (f"{{id:{js(f['id'])},sev:{js(f['sev'])},emp:{js(f['emp'])},ref:{js(f['ref'])},resolved:false,disp:'',note:'',\n"
            f"       title:{js(f['title'])},\n"
            f"       detail:{js(f['detail'])}}}")


def spiff_detail_line(d):
    note = f",note:{js(d['note'])}" if d.get("note") else ""
    return (f"{{date:{js(d['date'])},job:{js(d['job'])},customer:{js(d['customer'])},"
            f"type:{js(d['type'])},item:{js(d['item'])},spiff:{d['spiff']:g}{note}}}")


def comm_lead_line(l):
    return (f"{{id:{js(l['id'])},month:{js(l['month'])},tech:{js(l['tech'])},job:{js(l['job'])},"
            f"customer:{js(l['customer'])},status:{js(l['status'])},spiff:{l['spiff']:g},"
            f"payMonth:{js(l['payMonth'])},paid:{'true' if l['paid'] else 'false'}}}")


def carry_forward_line(c):
    return (f"{{id:{js(c['id'])},fromMonth:{js(c['fromMonth'])},emp:{js(c['emp'])},ref:{js(c['ref'])},"
            f"type:{js(c['type'])},amount:{c['amount']:g},dept:{js(c['dept'])},reason:{js(c['reason'])},"
            f"resolved:false,disposition:'',note:''}}")


lines = []
lines.append("const S={")
lines.append("  role:'billy',")
lines.append("  submissions:{steven:'not_started',caleb:'not_started'},")
lines.append("  pushback:{steven:null,caleb:null},")
lines.append("  revenue:[")
lines.append("    {dept:'MB Service-Residential',goal:123437,actual:null,se:true},")
lines.append("    {dept:'MB Service-Commercial', goal:216964,actual:null,se:true},")
lines.append("    {dept:'MB Plumbing',           goal:69934, actual:null,se:true},")
lines.append("    {dept:'CHS Service',           goal:150144,actual:null,se:false},")
lines.append("    {dept:'CHS Install-All',       goal:301651,actual:null,se:false},")
lines.append("    {dept:'MB Install-Residential',goal:349051,actual:null,se:false},")
lines.append("    {dept:'MB Install-Commercial', goal:299381,actual:null,se:false},")
lines.append("  ],")
lines.append("  bonuses:[")
lines.append("    {id:'jenny',name:'Jenny Miller',type:'CCS Supervisor Bonus',amount:0,dept:'Call Center',approved:false,note:'Pending — syncs from CCS Tracker sheet at page load'},")
def bonus_detail_line(d):
    return (f"{{job:{js(d['job'])},customer:{js(d['customer'])},item:{js(d['item'])},"
            f"soldOn:{js(d['soldOn'])},sale:{d['sale']:g},commission:{d['commission']:g}}}")

for b in r["bonuses"]:
    details = b.get("details") or []
    details_js = "[" + ",".join(bonus_detail_line(d) for d in details) + "]"
    lines.append(f"    {{id:{js(b['id'])},name:{js(b['name'])},type:{js(b['type'])},amount:{b['amount']:g},dept:{js(b['dept'])},approved:false,note:{js(b['note'])},details:{details_js}}},")
lines.append("  ],")

lines.append("  emps:{")
for mgr in ("steven", "caleb"):
    lines.append(f"    {mgr}:[")
    for e in r["emps"][mgr]:
        lines.append(f"      {emp_line(e)},")
    lines.append("    ],")
lines.append("  },")

lines.append("  flags:{")
for mgr in ("steven", "caleb"):
    lines.append(f"    {mgr}:[")
    for f in r["flags"][mgr]:
        lines.append(f"      {flag_line(f)},")
    lines.append("    ],")
lines.append("  },")

lines.append("  commLeads:[")
for l in r["commLeads"]:
    lines.append(f"    {comm_lead_line(l)},")
lines.append("  ],")

lines.append("  carryForward:[")
for c in r["carryForward"]:
    lines.append(f"    {carry_forward_line(c)},")
lines.append("  ],")

lines.append("  manuals:{steven:[],caleb:[]},")
lines.append("  jayJobs:[],")

lines.append("  officeMems:[")
for m in r["officeMems"]:
    lines.append(f"    {{name:{js(m['name'])},total:{m['total']:g},dept:'MB Residential Service'}},")
lines.append("  ],")

lines.append("  spiffDetail:{")
for mgr in ("steven", "caleb"):
    lines.append(f"    {mgr}:{{")
    for name, detail in r["spiffDetail"].get(mgr, {}).items():
        lines.append(f"      {js(name)}:[")
        for d in detail:
            lines.append(f"        {spiff_detail_line(d)},")
        lines.append("      ],")
    lines.append("    },")
lines.append("  },")

lines.append("  ingestion:[")
lines.append("    {name:'Master Pay File',    ok:true},")
lines.append("    {name:'Accessory Sales',    ok:true},")
lines.append("    {name:'Membership Report',  ok:true},")
lines.append("    {name:'Lead Request Report',ok:true},")
lines.append("    {name:'Tech Performance',   ok:true},")
lines.append("    {name:'Rich Commission',    ok:true},")
lines.append("    {name:'CCS Tracker',        ok:false,note:'Jenny has not submitted yet'},")
lines.append("  ]")
lines.append("};")

result = "\n".join(lines)
with open("full_S_block.js", "w", encoding="utf-8") as f:
    f.write(result)
print(f"Written full_S_block.js ({len(result)} chars, {len(lines)} lines)")
