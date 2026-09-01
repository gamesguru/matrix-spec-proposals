function Pandoc(doc)
  -- The Makefile supplies the first H1 as PDF metadata.  Removing it and
  -- promoting the remaining headings is only safe for documents that use a
  -- single H1 title.  Many established MSCs use H1 for their major sections.
  local h1_count = 0
  doc:walk({
    Header = function(header)
      if header.level == 1 then
        h1_count = h1_count + 1
      end
    end,
  })

  if h1_count ~= 1 then
    return doc
  end

  return doc:walk({
    Header = function(header)
      if header.level == 1 then
        return {}
      end

      header.level = header.level - 1
      return header
    end,
  })
end
