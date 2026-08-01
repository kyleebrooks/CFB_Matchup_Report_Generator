<?php
/**
 * report_proxy.php
 * Same-origin proxy for the AFPLNA matchup-report service (a plain-HTTP
 * DigitalOcean droplet). The scoreboard calls THIS file over HTTPS, and the
 * request is forwarded to the droplet server-side over HTTP.
 *
 * Why: once the site is served over HTTPS, a browser cannot call
 * http://143.198.20.72 directly (mixed content is blocked), and that droplet
 * does not support HTTPS. Proxying keeps the browser talking only to the
 * secure same origin, and keeps the service API key on the server.
 *
 * Proxied endpoints (?endpoint=):
 *   generate-report  (POST JSON)  -> queues a report, returns 202 + job handle
 *   report-status    (GET)        -> live progress: state/percent/message
 *   has-report       (GET)        -> {exists: bool}
 *   get-report       (GET)        -> the PDF (streamed back)
 */

// ---------------------------------------------------------------------------
// Session bootstrap
// ---------------------------------------------------------------------------
// scoreboard.php starts its session through common.inc, which may set a custom
// session name, cookie path or save handler. Calling session_start() cold here
// would open a DIFFERENT, empty session, so $_SESSION['username'] would never be
// found and every request would 401 with "You must be logged in." — even for a
// user who is plainly logged in. Bootstrap the same way the site does first.
if (session_status() !== PHP_SESSION_ACTIVE) {
    if (file_exists(__DIR__ . '/common.inc')) {
        include_once __DIR__ . '/common.inc';
    }
    if (function_exists('SessionStarted')) {
        @SessionStarted();
    }
    if (session_status() !== PHP_SESSION_ACTIVE) {
        @session_start();
    }
}

// The scoreboard is a members-only page; require a logged-in session.
if (!isset($_SESSION['username']) || $_SESSION['username'] === '') {
    http_response_code(401);
    header('Content-Type: application/json');
    // Say enough to tell "not logged in" apart from "the session did not load",
    // which look identical from the browser and are fixed in completely
    // different places.
    echo json_encode(array(
        'error'  => 'You must be logged in.',
        'reason' => 'no_session_username',
        'debug'  => array(
            'session_name'      => session_name(),
            'session_id'        => session_id() ? 'present' : 'missing',
            'cookie_received'   => isset($_COOKIE[session_name()]) ? 'yes' : 'no',
            'session_keys'      => array_keys($_SESSION),
            'common_inc_loaded' => file_exists(__DIR__ . '/common.inc') ? 'yes' : 'no',
            'hint'              => 'cookie_received=no means the browser sent no session cookie '
                                 . '(really logged out, or a cookie path/domain mismatch). '
                                 . 'cookie_received=yes with empty session_keys means this file '
                                 . 'opened a different session than the rest of the site.',
        ),
    ));
    exit;
}

// Droplet base (HTTP, server-side only — never sent to the browser).
$BASE = 'http://143.198.20.72';

$endpoint = isset($_GET['endpoint']) ? $_GET['endpoint'] : '';
$allowed = array('generate-report', 'report-status', 'has-report', 'get-report');
if (!in_array($endpoint, $allowed, true)) {
    http_response_code(400);
    header('Content-Type: application/json');
    echo json_encode(array('error' => 'Invalid or missing endpoint.', 'allowed' => $allowed));
    exit;
}

// Fetch the report-service API key server-side (API_KEYS.API_NAME='cfbmatchupreport').
$apiKey = '';
$conn = @mysqli_connect("p3nlmysql149plsk.secureserver.net", "kdogg4207", "xMkM2941", "kdogg4207");
if ($conn) {
    $res = @mysqli_query($conn, "SELECT `KEY` FROM API_KEYS WHERE API_NAME='cfbmatchupreport' LIMIT 1");
    if ($res && ($row = mysqli_fetch_assoc($res))) { $apiKey = trim($row['KEY']); }
    mysqli_close($conn);
}
if ($apiKey === '') {
    http_response_code(500);
    header('Content-Type: application/json');
    echo json_encode(array(
        'error' => 'Report service key not configured.',
        'detail' => "No 'cfbmatchupreport' row in API_KEYS, or the database was unreachable.",
    ));
    exit;
}

if ($endpoint === 'generate-report') {
    // Read the JSON body from the client, inject the key, forward as POST JSON.
    $raw = file_get_contents('php://input');
    $data = json_decode($raw, true);
    if (!is_array($data)) { $data = array(); }
    $data['api_key'] = $apiKey;

    $ch = curl_init($BASE . '/generate-report');
    curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
    curl_setopt($ch, CURLOPT_POST, true);
    curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($data));
    curl_setopt($ch, CURLOPT_HTTPHEADER, array('Content-Type: application/json', 'Accept: application/json'));
    // The service now queues the job and answers 202 in milliseconds, so this no
    // longer needs to outlast a multi-minute build. The read-timeout fallback
    // below is kept in case an older build of the service is still deployed.
    curl_setopt($ch, CURLOPT_TIMEOUT, 30);
    curl_setopt($ch, CURLOPT_CONNECTTIMEOUT, 10);
    $body    = curl_exec($ch);
    $code    = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $ctype   = curl_getinfo($ch, CURLINFO_CONTENT_TYPE);
    $connT   = curl_getinfo($ch, CURLINFO_CONNECT_TIME);
    $errno   = curl_errno($ch);
    $errmsg  = $errno ? curl_error($ch) : '';
    curl_close($ch);

    header('Cache-Control: no-store');
    header('Content-Type: application/json');

    // errno 28 = timeout. If we already connected (connect time > 0), it's a
    // READ timeout: the job is still generating on the droplet. Treat as accepted.
    if ($errno === 28 && $connT > 0) {
        http_response_code(202);
        echo json_encode(array(
            'ok' => true,
            'state' => 'running',
            'message' => 'Report generation started; this can take a few minutes.'
        ));
        exit;
    }
    // Any other curl error (e.g. could not connect) = service unreachable.
    if ($errno) {
        http_response_code(502);
        echo json_encode(array('error' => 'Report service unreachable: ' . $errmsg));
        exit;
    }
    // The service responded within the window — relay its response verbatim.
    http_response_code($code ? $code : 502);
    if ($ctype) { header('Content-Type: ' . $ctype); }
    echo $body;
    exit;
}

// ---- GET endpoints: report-status / has-report / get-report ----
$params = array(
    'api_key'   => $apiKey,
    'home_team' => isset($_GET['home_team']) ? $_GET['home_team'] : '',
    'away_team' => isset($_GET['away_team']) ? $_GET['away_team'] : '',
);
$url = $BASE . '/' . $endpoint . '?' . http_build_query($params);

// Capture response headers so we can relay content-type / disposition (PDF).
$respHeaders = array();
$ch = curl_init($url);
curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
// Status polling must stay snappy; a PDF download can take a while.
curl_setopt($ch, CURLOPT_TIMEOUT, $endpoint === 'get-report' ? 120 : 20);
curl_setopt($ch, CURLOPT_CONNECTTIMEOUT, 10);
curl_setopt($ch, CURLOPT_HEADERFUNCTION, function ($ch, $header) use (&$respHeaders) {
    $parts = explode(':', $header, 2);
    if (count($parts) === 2) { $respHeaders[strtolower(trim($parts[0]))] = trim($parts[1]); }
    return strlen($header);
});
$body  = curl_exec($ch);
$code  = curl_getinfo($ch, CURLINFO_HTTP_CODE);
$ctype = curl_getinfo($ch, CURLINFO_CONTENT_TYPE);
$err   = curl_errno($ch) ? curl_error($ch) : '';
curl_close($ch);

if ($err) {
    http_response_code(502);
    header('Content-Type: application/json');
    header('Cache-Control: no-store');
    echo json_encode(array('error' => 'Report service unreachable: ' . $err));
    exit;
}

http_response_code($code ? $code : 502);
header('Cache-Control: no-store');
if ($ctype) { header('Content-Type: ' . $ctype); }
// Relay the download filename for the PDF (or supply one).
if (isset($respHeaders['content-disposition'])) {
    header('Content-Disposition: ' . $respHeaders['content-disposition']);
} elseif (stripos((string)$ctype, 'pdf') !== false) {
    header('Content-Disposition: attachment; filename="AFPLNA_matchup_report.pdf"');
}
echo $body;
exit;
