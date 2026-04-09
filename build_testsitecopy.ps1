$root = Join-Path (Get-Location) 'testsitecopy'
$productsDir = Join-Path $root "products"
New-Item -ItemType Directory -Path $productsDir -Force | Out-Null

$base = "https://radiator-home-heat.ru"
$catalog = "https://radiator-home-heat.ru/?tab=7&roistat=direct4_context_17528380533_guardo%20retta&roistat_referrer=www.gismeteo.ru&roistat_pos=none_0&utm_source=yandex&utm_medium=cpc&utm_campaign=706184014&utm_content=17528380533&utm_term=guardo%20retta&y_ref=www.gismeteo.ru&yclid=11269741551940534271&ybaip=1"
$catalogHtml = (Invoke-WebRequest -Uri $catalog -UseBasicParsing).Content
$paths = [regex]::Matches($catalogHtml, 'href\s*=\s*"(/[a-z0-9_]+)"') |
    ForEach-Object { $_.Groups[1].Value } |
    Where-Object { $_ -notmatch '^/(agree|privacy)$' } |
    Sort-Object -Unique

$items = @()

foreach ($path in $paths) {
    $url = $base + $path
    try {
        $html = (Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 30).Content
    } catch {
        continue
    }

    $clean = [regex]::Replace($html, '<script[\s\S]*?</script>', ' ', 'IgnoreCase')
    $clean = [regex]::Replace($clean, '<style[\s\S]*?</style>', ' ', 'IgnoreCase')
    $text = [regex]::Replace($clean, '<[^>]+>', ' ')
    $text = [System.Net.WebUtility]::HtmlDecode($text)
    $text = [regex]::Replace($text, '\s+', ' ').Trim()

    $name = ''
    $h1 = [regex]::Match($clean, '<h1[^>]*>(.*?)</h1>', 'IgnoreCase,Singleline')
    if ($h1.Success) {
        $name = ([regex]::Replace([System.Net.WebUtility]::HtmlDecode($h1.Groups[1].Value), '<[^>]+>', '') -replace '\s+', ' ').Trim()
    }
    if ([string]::IsNullOrWhiteSpace($name)) { continue }

    $price = ''
    $pm = [regex]::Match($text, 'От\s+([0-9\s]+)\s+рублей', 'IgnoreCase')
    if ($pm.Success) { $price = (($pm.Groups[1].Value -replace '\s+', '').Trim()) }
    if (-not $price) {
        $pm2 = [regex]::Match($text, 'от\s+([0-9\s]+)\s*₽', 'IgnoreCase')
        if ($pm2.Success) { $price = (($pm2.Groups[1].Value -replace '\s+', '').Trim()) }
    }

    $img = ''
    $im = [regex]::Match($html, '<meta\s+property="og:image"\s+content="([^"]+)"', 'IgnoreCase')
    if ($im.Success) { $img = $im.Groups[1].Value }

    $meta = ''
    $md = [regex]::Match($html, '<meta\s+name="description"\s+content="([^"]*)"', 'IgnoreCase')
    if ($md.Success) { $meta = [System.Net.WebUtility]::HtmlDecode($md.Groups[1].Value) }

    $desc = ''
    $dm = [regex]::Match($text, 'Описание товара\s*Характеристики товара\s*(.*?)\s*Ширина\s*:', 'IgnoreCase')
    if ($dm.Success) {
        $desc = ($dm.Groups[1].Value -replace '\s+', ' ').Trim()
    } else {
        $dm2 = [regex]::Match($text, 'Описание товара\s*(.*?)\s*Характеристики товара', 'IgnoreCase')
        if ($dm2.Success) { $desc = ($dm2.Groups[1].Value -replace '\s+', ' ').Trim() }
    }

    $charBlock = ''
    $cm = [regex]::Match($text, 'Ширина\s*:.*?(?=ОСТАЛИСЬ ВОПРОСЫ|$)', 'IgnoreCase')
    if ($cm.Success) { $charBlock = ($cm.Value -replace '\s+', ' ').Trim() }
    if (-not $charBlock) {
        $cm2 = [regex]::Match($text, 'Характеристики\s*:\s*(.*?)\s*От\s+[0-9\s]+\s+рублей', 'IgnoreCase')
        if ($cm2.Success) { $charBlock = ($cm2.Groups[1].Value -replace '\s+', ' ').Trim() }
    }

    $slug = $path.TrimStart('/')
    $localPage = "products/$slug.html"

    $safeName = [System.Net.WebUtility]::HtmlEncode($name)
    $safeImg = [System.Net.WebUtility]::HtmlEncode($img)
    $safeDesc = [System.Net.WebUtility]::HtmlEncode($desc)
    $safeMeta = [System.Net.WebUtility]::HtmlEncode($meta)
    $safePrice = if ($price) { "от $price ₽" } else { "Цена по запросу" }
    $safePrice = [System.Net.WebUtility]::HtmlEncode($safePrice)
    $safeChar = [System.Net.WebUtility]::HtmlEncode($charBlock)
    $safeSrc = [System.Net.WebUtility]::HtmlEncode($url)

    $detail = @"
<!doctype html>
<html lang='ru'>
<head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>$safeName</title>
<style>body{margin:0;background:#f3f5f7;color:#111827;font-family:Arial,sans-serif}.wrap{max-width:980px;margin:0 auto;padding:20px}.card{background:#fff;border:1px solid #e5e7eb;border-radius:14px;overflow:hidden}.hero{display:grid;grid-template-columns:320px 1fr;gap:0}.hero img{width:100%;height:100%;object-fit:cover;background:#fff}.pad{padding:18px}.name{margin:0 0 8px}.price{margin:0;color:#b91c1c;font-size:24px;font-weight:700}.section{margin-top:14px;padding:16px;background:#fff;border:1px solid #e5e7eb;border-radius:14px}.pre{white-space:pre-wrap;line-height:1.45}.back{display:inline-block;margin-bottom:14px;padding:10px 12px;background:#2563eb;color:#fff;text-decoration:none;border-radius:10px}.src{font-size:14px;color:#4b5563}</style>
</head>
<body>
<div class='wrap'>
<a class='back' href='../index.html'>← Вернуться в каталог</a>
<article class='card'>
<div class='hero'>
<img src='$safeImg' alt='$safeName'>
<div class='pad'>
<h1 class='name'>$safeName</h1>
<p class='price'>$safePrice</p>
<p>$safeMeta</p>
<p class='src'>Источник: <a href='$safeSrc' target='_blank' rel='noopener'>$safeSrc</a></p>
</div>
</div>
</article>
<section class='section'>
<h2>Описание</h2>
<div class='pre'>$safeDesc</div>
</section>
<section class='section'>
<h2>Технические характеристики</h2>
<div class='pre'>$safeChar</div>
</section>
</div>
</body>
</html>
"@

    [System.IO.File]::WriteAllText((Join-Path $root $localPage), $detail, [System.Text.Encoding]::UTF8)
    $items += [PSCustomObject]@{
        name  = $name
        price = $price
        image = $img
        tech  = $charBlock
        page  = $localPage
    }
}

$sb = New-Object System.Text.StringBuilder
[void]$sb.AppendLine('<!doctype html>')
[void]$sb.AppendLine('<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Каталог трубчатых радиаторов</title>')
[void]$sb.AppendLine('<style>body{margin:0;font-family:Arial,sans-serif;background:#f3f5f7;color:#1f2937}.wrap{max-width:1240px;margin:0 auto;padding:24px}header{background:#111827;color:#fff}h1{margin:0;font-size:32px}p{line-height:1.5}.lead{color:#d1d5db}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:18px}.card{background:#fff;border:1px solid #e5e7eb;border-radius:14px;overflow:hidden;box-shadow:0 4px 16px rgba(0,0,0,.06)}.card img{width:100%;height:240px;object-fit:cover;background:#fff}.card-body{padding:14px}.name{font-size:18px;font-weight:700;margin:0 0 8px}.price{font-size:20px;color:#b91c1c;font-weight:700;margin:0 0 10px}.tech{font-size:13px;color:#374151}.btn{display:inline-block;margin-top:12px;padding:10px 12px;background:#2563eb;color:#fff;text-decoration:none;border-radius:10px}.section{margin:26px 0;padding:18px;background:#fff;border:1px solid #e5e7eb;border-radius:14px}</style></head><body>')
[void]$sb.AppendLine('<header><div class="wrap"><h1>Каталог трубчатых радиаторов</h1><p class="lead">Сравнение моделей по характеристикам, размерам и стоимости.</p></div></header><main class="wrap">')
[void]$sb.AppendLine('<section class="section"><h2>Подбор радиаторов</h2><p>В каталоге собраны модели с сохраненными наименованиями и техническими параметрами. Для детального изучения нажмите «Открыть источник» — откроется локальная карточка товара с характеристиками.</p></section>')
[void]$sb.AppendLine('<section class="grid">')

foreach ($it in $items) {
    $n = [System.Net.WebUtility]::HtmlEncode($it.name)
    $i = [System.Net.WebUtility]::HtmlEncode($it.image)
    $t = [System.Net.WebUtility]::HtmlEncode($it.tech)
    $p = if ($it.price) { "от $($it.price) ₽" } else { "Цена по запросу" }
    $p = [System.Net.WebUtility]::HtmlEncode($p)
    $l = [System.Net.WebUtility]::HtmlEncode($it.page)
    [void]$sb.AppendLine('<article class="card">')
    if ($i) { [void]$sb.AppendLine('<img src="' + $i + '" alt="' + $n + '">') }
    [void]$sb.AppendLine('<div class="card-body"><h3 class="name">' + $n + '</h3><p class="price">' + $p + '</p><p class="tech">' + $t + '</p><a class="btn" href="' + $l + '">Открыть источник</a></div></article>')
}

[void]$sb.AppendLine('</section></main></body></html>')
[System.IO.File]::WriteAllText((Join-Path $root 'index.html'), $sb.ToString(), [System.Text.Encoding]::UTF8)

Write-Output ("Detail pages: " + $items.Count)
Write-Output ("Updated: " + (Join-Path $root 'index.html'))
