def okname: test("\\A[A-Za-z0-9_]+\\z");
. as $doc
| [paths(type != "object")
   | select(all(.[]; type == "string"))
   | select(.[0] != "haiku")
   | select(.[0] != "_comments")] as $all
| [$all[] | . as $p | select((($doc | getpath($p) | type) == "array") | not)] as $scal
# #1136: an array of names is carried as its own row, under "<key>__arr", so
# no string value can ever be mistaken for one. An array whose key is outside
# the alphabet is simply not carried: it never causes that refusal and is not
# part of the collision check below (only carried arrays are).
| [$all[] | . as $p | select(($doc | getpath($p) | type) == "array") | select(all(.[]; okname))] as $arrs
# A top-level object also gets a "<key>__obj" marker row, so a caller can tell
# an object (or an empty one) sitting where it expected a list or a scalar.
| [$doc | if type == "object" then to_entries[] else empty end
   | select(.key != "haiku" and .key != "_comments" and (.key | okname) and ((.value | type) == "object"))
   | .key] as $objs
| if (($scal | flatten) | any(okname | not)) then
    "#refuse a config key is outside [A-Za-z0-9_]"
  elif ((($scal + $arrs) | map(join("_"))) as $names
        | ($names | unique | length) != ($names | length)) then
    "#refuse two config keys flatten to the same name"
  # the generated row names own the __arr / __obj suffixes
  elif ((($scal + $arrs) | map(join("_"))) | any(endswith("__arr") or endswith("__obj"))) then
    "#refuse a config key ends in a reserved suffix (__arr or __obj)"
  elif ([$scal[] as $p | $doc | getpath($p) | select(type == "string" and test("[\t\n]"))] | length) > 0 then
    "#refuse a config value contains a tab or a newline"
  else
    ([$scal[] as $p | ($doc | getpath($p)) as $v | select($v != null)
      | ($p | join(".")) + "\t" + ($v | tostring)]
     + [$arrs[] as $p | ($doc | getpath($p)) as $v
      | ($p | join(".")) + "__arr\t"
        + (if any($v[]; (type != "string") or . == "" or test("[ \t\n\r]")) then "#invalid"
           else "=" + ($v | join(" ")) end)]
     + [$objs[] | . + "__obj\t#object"])[]
  end
