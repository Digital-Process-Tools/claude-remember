. as $doc
| [paths(type != "object")
   | select(all(.[]; type == "string"))
   | select(.[0] != "haiku")
   | select(.[0] != "_comments")
   | select(. as $p | (($doc | getpath($p) | type) != "array")
                      or ($p | all(test("^[A-Za-z0-9_]+$"))))] as $ks
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
    | ($p | join(".")) + "\t" +
      (if ($v | type) == "array" then
         # #1136: an array of plain names is carried as one space-joined value
         # ("[]" when empty, so an empty list differs from an absent key); any
         # other array is marked rather than guessed at.
         (if any($v[]; (type != "string") or test("[ \t\n\r]")) then "#invalid"
          elif ($v | length) == 0 then "[]"
          else ($v | join(" ")) end)
       else ($v | tostring) end)
  end
