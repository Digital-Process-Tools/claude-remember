. as $doc
| [paths(type != "object")
   | select(all(.[]; type == "string"))
   | select(.[0] != "haiku")
   | select(.[0] != "_comments")] as $all
| [$all[] | . as $p | select(($doc | getpath($p) | type) != "array")] as $ks
# #1136: an array of names is carried as one row, but it can never cause a
# refusal and never displaces a scalar: one whose key is outside the alphabet,
# or whose flattened name another key already holds, is simply not carried.
| ($ks | map(join("_"))) as $taken
| [$all[]
   | . as $p
   | select(($doc | getpath($p) | type) == "array")
   | select(all(.[]; test("^[A-Za-z0-9_]+$")))
   | select(join("_") as $n | all($taken[]; . != $n))] as $arrs0
| ($arrs0 | map(join("_"))) as $an
| [$arrs0[] | select(join("_") as $n | ([$an[] | select(. == $n)] | length) == 1)] as $arrs
| if (($ks | flatten) | any(test("^[A-Za-z0-9_]+$") | not)) then
    "#refuse a config key is outside [A-Za-z0-9_]"
  elif (($ks | map(join("_")) | unique | length) != ($ks | length)) then
    "#refuse two config keys flatten to the same name"
  elif ([$ks[] as $p | $doc | getpath($p) | select(type == "string" and test("[\t\n]"))] | length) > 0 then
    "#refuse a config value contains a tab or a newline"
  else
    ($ks + $arrs)[] as $p
    | ($doc | getpath($p)) as $v
    | select($v != null)
    | ($p | join(".")) + "\t" +
      (if ($v | type) == "array" then
         # "[a b]" for an array of single plain names, "[]" when empty, and
         # "#invalid" for any other array: marked rather than guessed at.
         (if any($v[]; (type != "string") or test("[ \t\n\r]")) then "#invalid"
          else "[" + ($v | join(" ")) + "]" end)
       else ($v | tostring) end)
  end
