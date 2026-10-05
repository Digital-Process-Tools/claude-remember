. as $doc
| [paths(type != "object" and type != "array")
   | select(all(.[]; type == "string"))
   | select(.[0] != "haiku")] as $ks
| if (($ks | flatten) | any(test("^[A-Za-z0-9_]+$") | not)) then
    "#refuse a config key is outside [A-Za-z0-9_]"
  elif (($ks | map(join("_")) | unique | length) != ($ks | length)) then
    "#refuse two config keys flatten to the same name"
  elif ([$ks[] as $p | $doc | getpath($p) | select(type == "string" and test("[\t\n]"))] | length) > 0 then
    "#refuse a config value contains a tab or a newline"
  else
    $ks[] as $p
    | ($doc | getpath($p)) as $v
    | select($v != null)
    | ($p | join(".")) + "\t" + ($v | tostring)
  end
