<?php
// Vercel PHP function for the existing Dompdf output path.
// Called by the Python API; protect it with the same server-side secret.
require __DIR__ . '/../vendor/autoload.php';

use Dompdf\Dompdf;
use Dompdf\Options;

header('Content-Type: application/pdf');
$token = getenv('PDF_RENDER_TOKEN') ?: '';
$provided = $_SERVER['HTTP_AUTHORIZATION'] ?? '';
if ($token === '' || !hash_equals('Bearer ' . $token, $provided)) {
    http_response_code(401);
    header('Content-Type: application/json');
    echo json_encode(['detail' => 'Unauthorized']);
    exit;
}
$payload = json_decode(file_get_contents('php://input'), true);
$html = is_array($payload) ? ($payload['html'] ?? '') : '';
if (!is_string($html) || $html === '') {
    http_response_code(400);
    header('Content-Type: application/json');
    echo json_encode(['detail' => 'Missing HTML']);
    exit;
}

$options = new Options();
$options->set('isHtml5ParserEnabled', true);
$options->set('isRemoteEnabled', false);
$options->set('defaultFont', 'DejaVu Sans');
$dompdf = new Dompdf($options);
$dompdf->loadHtml($html, 'UTF-8');
$dompdf->setPaper('A4', 'portrait');
$dompdf->render();
echo $dompdf->output();
