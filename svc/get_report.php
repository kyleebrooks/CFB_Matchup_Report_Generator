<?php
declare(strict_types=1);
require_once __DIR__ . '/../api_keys.php';
header('Cache-Control: no-store');

$BASE = 'http://143.198.20.72';
$home = $_GET['home_team'] ?? '';
$away = $_GET['away_team'] ?? '';
$q = http_build_query(['home_team'=>$home,'away_team'=>$away,'_'=>($_GET['_'] ?? time())]);

$ch = curl_init("$BASE/get-report?$q");
curl_setopt_array($ch, [
  CURLOPT_RETURNTRANSFER => true,
  CURLOPT_FOLLOWLOCATION => true,
  CURLOPT_CONNECTTIMEOUT => 10,
  CURLOPT_TIMEOUT        => 30,
  CURLOPT_HTTPHEADER     => [
    'Accept: application/pdf',
    'Authorization: Bearer ' . $AFPLNA_API_KEY,
    'User-Agent: Mozilla/5.0'
  ],
]);
$body = curl_exec($ch);
$code = curl_getinfo($ch, CURLINFO_HTTP_CODE) ?: 502;
$contentType = curl_getinfo($ch, CURLINFO_CONTENT_TYPE) ?: 'application/pdf';
if ($body === false) { http_response_code(502); header('Content-Type: application/json; charset=utf-8'); echo json_encode(['error'=>'upstream','detail'=>curl_error($ch)]); exit; }
http_response_code($code);
header('Content-Type: ' . $contentType);
echo $body;
