#!/usr/bin/env bash
# create-rule.sh — Create a CDQ rule specification end-to-end.
#
# Flow (matches what the IDMC UI does, captured from DevTools):
#   1. POST  /frs/api/v1/Documents             → mint metadata shell, get id
#   2. PATCH /frs/v1/Documents('id')           → attach ruleModel via nativeData.documentBlob
#   3. GET   to verify FRS + rule-service both see the rule
#
# Usage:
#   ./create-rule.sh <rule_name> [description] [field_name] [dimension] [flags]
#
# Flags (anywhere in the arg list):
#   --rule-template <file>   Use ruleModel JSON from <file> (see examples/).
#                            Without this flag, a built-in null-check is used.
#   --dry-run                Build the bodies and print the PATCH body to stdout,
#                            but do NOT POST or PATCH anything.
#
# Defaults: description="Created by create-rule.sh", field_name="Input",
#           dimension="COMPLETENESS". When --rule-template is used, the
#           template's options[DIMENSION] takes precedence over the CLI arg
#           (CLI dimension only matters when no template is supplied).
#
# Parent location is the same Space/Project used by INCEPT_TEST_NULL_CHECK.
# Override via env vars CDQ_SPACE_ID, CDQ_SPACE_NAME, CDQ_PROJECT_ID,
# CDQ_PROJECT_NAME.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Argument parsing -------------------------------------------------------
RULE_TEMPLATE_FILE=""
DRY_RUN=0
AUTO_UUID=0
POSITIONAL=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --rule-template)
      RULE_TEMPLATE_FILE="${2:?--rule-template needs a file path}"
      [[ -f "$RULE_TEMPLATE_FILE" ]] || { echo "error: template not found: $RULE_TEMPLATE_FILE" >&2; exit 1; }
      shift 2 ;;
    --dry-run)   DRY_RUN=1;   shift ;;
    --auto-uuid) AUTO_UUID=1; shift ;;
    --) shift; while [[ $# -gt 0 ]]; do POSITIONAL+=("$1"); shift; done ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//; /^set -euo/d'; exit 0 ;;
    *) POSITIONAL+=("$1"); shift ;;
  esac
done
set -- "${POSITIONAL[@]}"

RULE_NAME="${1:?usage: $0 <rule_name> [description] [field_name] [dimension] [--rule-template f] [--dry-run]}"
DESCRIPTION="${2:-Created by create-rule.sh}"
FIELD_NAME="${3:-Input}"
DIMENSION="${4:-COMPLETENESS}"

CDQ_SPACE_ID="${CDQ_SPACE_ID:-7cCn5thwWFLhiZoSosphKL}"
CDQ_SPACE_NAME="${CDQ_SPACE_NAME:-REG}"
CDQ_PROJECT_ID="${CDQ_PROJECT_ID:-a3DaqI5cWMAfKahwNbNTcP}"
CDQ_PROJECT_NAME="${CDQ_PROJECT_NAME:-Teradyne_CDQ_Training}"

# --- Build the ruleModel object (template-aware) ----------------------------
# NEW_ID is unknown until after POST, so we build the model with a placeholder
# and substitute the real id later. Use a sentinel that won't collide.
ID_PLACEHOLDER="@@__NEW_DOC_ID__@@"

if [[ -n "$RULE_TEMPLATE_FILE" ]]; then
  # Template path: load the file, override identity fields. DIMENSION is
  # pulled from the template's options (template wins).
  RULE_MODEL_OBJ="$(jq -c \
    --arg id   "$ID_PLACEHOLDER" \
    --arg name "$RULE_NAME" \
    --arg desc "$DESCRIPTION" \
    '. + {"$$IID":$id, "$$id":$id, name:$name, description:$desc}' \
    "$RULE_TEMPLATE_FILE")"
  TPL_DIM="$(jq -r '(.options[]? | select(.name=="DIMENSION") | .optionValue) // empty' "$RULE_TEMPLATE_FILE")"
  [[ -n "$TPL_DIM" ]] && DIMENSION="$TPL_DIM"

  # --auto-uuid: rewrite every $$externalID to a fresh UUID. Because
  # ##externalID references share the same UUID string, a textual replace
  # catches both naming conventions in one pass.
  if [[ "$AUTO_UUID" -eq 1 ]]; then
    OLD_UUIDS="$(echo "$RULE_MODEL_OBJ" | jq -r '
      [.. | objects | .["$$externalID"]? // empty]
      | map(select(. != "undefined" and . != ""))
      | unique | .[]
    ')"
    while IFS= read -r old; do
      [[ -z "$old" ]] && continue
      new="$(uuidgen | tr 'A-Z' 'a-z')"
      RULE_MODEL_OBJ="${RULE_MODEL_OBJ//$old/$new}"
    done <<< "$OLD_UUIDS"
  fi
else
  # Built-in default: null-check on a single string field, COMPLETENESS dim.
  INPUT_UUID="$(uuidgen | tr 'A-Z' 'a-z')"
  OUTPUT_UUID="$(uuidgen | tr 'A-Z' 'a-z')"
  RULE_MODEL_OBJ="$(jq -nc \
    --arg id          "$ID_PLACEHOLDER" \
    --arg name        "$RULE_NAME" \
    --arg desc        "$DESCRIPTION" \
    --arg field_name  "$FIELD_NAME" \
    --arg input_uuid  "$INPUT_UUID" \
    --arg output_uuid "$OUTPUT_UUID" \
    --arg dimension   "$DIMENSION" \
  '{
    "$$class":"com.informatica.dq.rulebuilder.RuleDefinition",
    "$$IID": $id, "$$id": $id,
    name: $name, description: $desc,
    outsideValidityMessage:"undefined",
    validFromDate:"-3600000", validToDate:"-3600000",
    "$$aggregator":{
      "$$lockedOn":0, "$$version":1494513349121,
      "##IID":"U:IGWzIjZXEeefFIPeKxPNig",
      name:"DATES", "$$lockedBy":"", "$$class":789, "$$property":"contents"
    },
    tags:[],
    options:[
      {"$$class":"com.informatica.dq.rulebuilder.StringOption", name:"DEFAULT_STRING_PRECISION",  optionValue:"100"},
      {"$$class":"com.informatica.dq.rulebuilder.StringOption", name:"DEFAULT_DECIMAL_PRECISION", optionValue:"10"},
      {"$$class":"com.informatica.dq.rulebuilder.StringOption", name:"DEFAULT_DECIMAL_SCALE",     optionValue:"4"},
      {"$$class":"com.informatica.dq.rulebuilder.StringOption", name:"DIMENSION",                 optionValue:$dimension},
      {"$$class":"com.informatica.dq.rulebuilder.StringOption", name:"EXCEPTION",                 optionValue:"false"}
    ],
    fields:[{
      "$$class":"com.informatica.dq.rulebuilder.Field",
      "$$id":"5",
      "$$externalID": $input_uuid,
      precision:"50", scale:"0",
      name: $field_name,
      "$type":{"##SID":"smd:com.informatica.metadata.seed.platform.Platform.typesystem/string","$$class":"com.informatica.metadata.common.typesystem.DataType"},
      description:""
    }],
    outputFields:[],
    testData:[],
    topRuleFamily:{
      "$$class":"com.informatica.dq.rulebuilder.RuleFamily",
      name:"PrimaryRuleSet", description:"",
      "$$id":"6", "$$externalID": $output_uuid,
      outputs:[], outputLinks:[],
      statements:[
        {
          "$$class":"com.informatica.dq.rulebuilder.Statement",
          action:{
            "$$class":"com.informatica.dq.rulebuilder.Operation",
            "$$id":"7", name:"SetField", description:"", type:"Valid",
            options:[{"$$class":"com.informatica.dq.rulebuilder.StringOption", name:"Value", optionValue:"VALID"}],
            inputs:[], suboperations:[], outputs:[]
          },
          condition:{
            "$$class":"com.informatica.dq.rulebuilder.Operation",
            "$$id":"8", name:"NotEquals", description:"", type:"NotEquals",
            options:[{"$$class":"com.informatica.dq.rulebuilder.StringOption", name:"useNull", optionValue:"true"}],
            inputs:[{name:$field_name,"$$class":"com.informatica.dq.rulebuilder.Field","##id":"5","##externalID":"undefined"}],
            suboperations:[], outputs:[]
          }
        },
        {
          "$$class":"com.informatica.dq.rulebuilder.Statement",
          action:   {"$$class":"com.informatica.dq.rulebuilder.Operation","$$id":"9",  name:"", description:"",                       options:[], inputs:[], suboperations:[], outputs:[]},
          condition:{"$$class":"com.informatica.dq.rulebuilder.Operation","$$id":"10", name:"", description:"", type:"DefaultValue", options:[], inputs:[], suboperations:[], outputs:[]}
        }
      ],
      ruleFamilies:[],
      fields:[{name:$field_name,"$$class":"com.informatica.dq.rulebuilder.Field","##id":"5","##externalID":$input_uuid}]
    }
  }')"
fi

# --- Session (skipped on --dry-run) -----------------------------------------
FRS_API="https://usw1.dmp-us.informaticacloud.com/frs/api/v1"
FRS_V1="https://usw1.dmp-us.informaticacloud.com/frs/v1"
RS="https://usw1-dqcloud.dmp-us.informaticacloud.com/rule-service/api/v1"

if [[ "$DRY_RUN" -eq 0 ]]; then
  eval "$("${SCRIPT_DIR}/refresh-session.sh")" >/dev/null
  H_AUTH=(-H "IDS-SESSION-ID: $IDMC_SESSION_ID")
  H_JSON=("${H_AUTH[@]}" -H 'Accept: application/json' -H 'Content-Type: application/json')
fi

# --- Step 1: Create FRS metadata shell (or skip on dry-run) -----------------
if [[ "$DRY_RUN" -eq 0 ]]; then
  echo ">>> [1/3] POST $FRS_API/Documents  (create metadata shell)"
  DOC_BODY="$(jq -nc \
    --arg name      "$RULE_NAME" \
    --arg desc      "$DESCRIPTION" \
    --arg sid       "$CDQ_SPACE_ID"   --arg sname "$CDQ_SPACE_NAME" \
    --arg pid       "$CDQ_PROJECT_ID" --arg pname "$CDQ_PROJECT_NAME" \
    --arg dimension "$DIMENSION" \
  '{
    documentType: "RULE_SPECIFICATION",
    name:         $name,
    description:  $desc,
    parentInfo: [
      {parentType:"Space",   parentId:$sid, parentName:$sname},
      {parentType:"Project", parentId:$pid, parentName:$pname}
    ],
    customAttributes: {
      stringAttrs: [
        {name:"DIMENSION",                   value:$dimension},
        {name:"EXCEPTION",                   value:"false"},
        {name:"ReferencedPublishingAllowed", value:"true"}
      ],
      numberAttrs: [], dateAttrs: []
    }
  }')"

  POST_RESP="$(curl -sS -X POST "$FRS_API/Documents" "${H_JSON[@]}" -d "$DOC_BODY" \
    -w $'\n%{http_code}')"
  POST_CODE="${POST_RESP##*$'\n'}"
  POST_JSON="${POST_RESP%$'\n'*}"
  NEW_ID="$(jq -r '.id // empty' <<< "$POST_JSON")"

  if [[ "$POST_CODE" != "201" || -z "$NEW_ID" ]]; then
    echo "ERROR: create failed (HTTP $POST_CODE)" >&2
    echo "$POST_JSON" >&2
    exit 1
  fi
  echo "    new id: $NEW_ID"
else
  NEW_ID="DRY_RUN_PLACEHOLDER_ID"
  echo ">>> [DRY RUN] would POST $FRS_API/Documents — using id $NEW_ID for body assembly"
fi

# --- Substitute the real id into the rule model -----------------------------
# (we built the model with ID_PLACEHOLDER; now replace with NEW_ID)
RULE_MODEL="${RULE_MODEL_OBJ//$ID_PLACEHOLDER/$NEW_ID}"

# --- Build documentBlob (derive inputFields/outputFields from ruleModel) ----
DOC_BLOB="$(jq -nc --argjson rm "$RULE_MODEL" --arg rms "$RULE_MODEL" '
  def field_type(f): (f["$type"]["##SID"] // "") | split("/") | .[-1] // "string";
  {
    inputFields: ($rm.fields | map({
      name: .name,
      type: field_type(.),
      precision: (.precision | tonumber? // 50),
      scale:     (.scale     | tonumber? // 0),
      id: .["$$externalID"]
    })),
    outputFields: [{
      name: ($rm.topRuleFamily.name // "PrimaryRuleSet"),
      id:   $rm.topRuleFamily["$$externalID"],
      type: "string",
      precision: "100",
      scale: 0
    }],
    ruleModel: $rms
  }')"

# --- Build outer PATCH body -------------------------------------------------
PATCH_BODY="$(jq -nc \
  --arg id        "$NEW_ID" \
  --arg name      "$RULE_NAME" \
  --arg desc      "$DESCRIPTION" \
  --arg blob      "$DOC_BLOB" \
  --arg dimension "$DIMENSION" \
'{
  name:         $name,
  description:  $desc,
  documentType: "RULE_SPECIFICATION",
  nativeData:   {documentBlob: $blob},
  docRef:       {docRefIds: []},
  customAttributes: {stringAttrs: [
    {name:"ReferencedPublishingAllowed", value:"true"},
    {name:"DIMENSION",                   value:$dimension},
    {name:"EXCEPTION",                   value:"false"}
  ]},
  documentState: "VALID",
  id:            $id
}')"

# --- Step 2: PATCH (or print on dry-run) ------------------------------------
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo ">>> [DRY RUN] would PATCH $FRS_V1/Documents('$NEW_ID')"
  echo ">>> assembled PATCH body (pretty):"
  jq . <<< "$PATCH_BODY"
  exit 0
fi

echo ">>> [2/3] PATCH $FRS_V1/Documents('$NEW_ID')  (attach rule body)"
PATCH_RESP="$(curl -sS -X PATCH "$FRS_V1/Documents('$NEW_ID')" "${H_JSON[@]}" -d "$PATCH_BODY" \
  -w $'\n%{http_code}')"
PATCH_CODE="${PATCH_RESP##*$'\n'}"
PATCH_BODY_OUT="${PATCH_RESP%$'\n'*}"

if [[ "$PATCH_CODE" != "200" && "$PATCH_CODE" != "204" ]]; then
  echo "ERROR: PATCH failed (HTTP $PATCH_CODE)" >&2
  echo "$PATCH_BODY_OUT" >&2
  echo "(metadata shell remains at id $NEW_ID — delete with:" >&2
  echo "  curl -X DELETE \"$FRS_API/Documents('$NEW_ID')\" -H \"IDS-SESSION-ID: \$IDMC_SESSION_ID\"  )" >&2
  exit 1
fi
echo "    PATCH ok (HTTP $PATCH_CODE)"

# --- Step 3: Verify ---------------------------------------------------------
echo ">>> [3/3] verify"
DOC_STATE="$(curl -sS "$FRS_API/Documents('$NEW_ID')" "${H_AUTH[@]}" -H 'Accept: application/json' \
  | jq -r '.documentState // "?"')"
RULE_HTTP="$(curl -sS -o /tmp/created_rule.json -w '%{http_code}' "$RS/Rules('$NEW_ID')" \
  "${H_AUTH[@]}" -H 'Accept: application/json')"
RULE_BYTES="$(stat -f%z /tmp/created_rule.json 2>/dev/null || stat -c%s /tmp/created_rule.json)"

echo "    FRS documentState  : $DOC_STATE"
echo "    rule-service GET   : HTTP $RULE_HTTP  ($RULE_BYTES bytes)"

cat <<EOF

==================================================
Created rule '$RULE_NAME'
  id              : $NEW_ID
  parent          : Space=$CDQ_SPACE_NAME / Project=$CDQ_PROJECT_NAME
  dimension       : $DIMENSION
  template        : ${RULE_TEMPLATE_FILE:-<built-in null-check>}
  documentState   : $DOC_STATE
==================================================

To view in UI:
  https://usw1-dqcloud.dmp-us.informaticacloud.com/dq-product/cloud/main/rulebuilder/$NEW_ID

To delete:
  eval "\$(./refresh-session.sh)" >/dev/null
  curl -X DELETE "$FRS_API/Documents('$NEW_ID')" -H "IDS-SESSION-ID: \$IDMC_SESSION_ID"
EOF
