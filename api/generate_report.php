<?php
declare(strict_types=1);
header('Content-Type: application/json; charset=utf-8');

$BASE = 'http://143.198.20.72';
$ch = curl_init("$BASE/generate-report");
curl_setopt_array($ch, [
  CURLOPT_POST=>true,
  CURLOPT_POSTFIELDS=>file_get_contents('php://input'), // we’ll send JSON or form-URL-encoded verbatim
  CURLOPT_HTTPHEADER=>[
    'Content-Type: ' . ($_SERVER['CONTENT_TYPE'] ?? 'application/json')
  ],
  CURLOPT_RETURNTRANSFER=>true,
  CURLOPT_FOLLOWLOCATION=>true,
  CURLOPT_CONNECTTIMEOUT=>10,
  CURLOPT_TIMEOUT=>600
]);
$body = curl_exec($ch);
$code = curl_getinfo($ch, CURLINFO_HTTP_CODE) ?: 502;
if ($body === false) {
  http_response_code(502);
  echo json_encode(['error'=>'upstream','detail'=>curl_error($ch)]);
  exit;
}
http_response_code($code);
echo $body;
