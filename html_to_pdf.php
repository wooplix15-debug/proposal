<?php
// Wooplix Proposal Agent — HTML to PDF via dompdf.
// Usage: php html_to_pdf.php INPUT.html OUTPUT.pdf
require __DIR__ . '/vendor/autoload.php';

use Dompdf\Dompdf;
use Dompdf\Options;

$in  = $argv[1] ?? null;
$out = $argv[2] ?? null;
if (!$in || !$out) {
    fwrite(STDERR, "usage: php html_to_pdf.php INPUT.html OUTPUT.pdf\n");
    exit(1);
}
$html = @file_get_contents($in);
if ($html === false) {
    fwrite(STDERR, "cannot read input: $in\n");
    exit(1);
}

$options = new Options();
$options->set('isHtml5ParserEnabled', true);
$options->set('isRemoteEnabled', false);
$options->set('defaultFont', 'DejaVu Sans');

$dompdf = new Dompdf($options);
$dompdf->loadHtml($html, 'UTF-8');
$dompdf->setPaper('A4', 'portrait');
$dompdf->render();
$canvas = $dompdf->getCanvas();
$font = $dompdf->getFontMetrics()->getFont('DejaVu Sans', 'normal');
$canvas->page_text(490, 814, 'Page {PAGE_NUM} of {PAGE_COUNT}', $font, 7, [0.39, 0.45, 0.55]);

if (file_put_contents($out, $dompdf->output()) === false) {
    fwrite(STDERR, "cannot write output: $out\n");
    exit(1);
}
fwrite(STDOUT, "PDF written: $out\n");
