<?php
declare(strict_types=1);
header('Content-Type: application/json; charset=utf-8');

$BASE = 'http://143.198.20.72';
$api_key   = $_GET['api_key']   ?? '';
$home_team = $_GET['home_team'] ?? '';
$away_team = $_GET['away_team'] ?? '';
$q = http_build_query([
  'api_key'=>$api_key, 'home_team'=>$home_team, 'away_team'=>$away_team, '_'=>($_GET['_'] ?? time())
]);

$ch = curl_init("$BASE/has-report?$q");
curl_setopt_array($ch, [
  CURLOPT_RETURNTRANSFER=>true,
  CURLOPT_FOLLOWLOCATION=>true,
  CURLOPT_CONNECTTIMEOUT=>10,
  CURLOPT_TIMEOUT=>30,
  CURLOPT_HTTPHEADER=>['Accept: application/json'],
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
