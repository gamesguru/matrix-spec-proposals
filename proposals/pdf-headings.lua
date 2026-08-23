local consumed_title = false

function Header(header)
  if not consumed_title and header.level == 1 then
    consumed_title = true
    return {}
  end

  if header.level > 1 then
    header.level = header.level - 1
  end

  return header
end
