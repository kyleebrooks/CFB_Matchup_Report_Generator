<?php
include 'common.inc';
if (!SessionStarted())
    $ses = 'true';
else
    $ses = 'false';
require_once 'api_keys.php';

// Connect to the database using the legacy mysql extension
$connection = mysql_connect(
    "p3nlmysql149plsk.secureserver.net",
    "kdogg4207",
    "xMkM2941"
);
if (!$connection) {
    die("Could not connect to database server");
}
if (!mysql_select_db("kdogg4207", $connection)) {
    die("Could not select database");
}

// Get current week and year
$weekID = null;
$year   = null;
$weekResult = mysql_query("SELECT weekID FROM week WHERE currentWeek='true'", $connection) or die('Query failed.');
if ($row = mysql_fetch_assoc($weekResult)) {
    $weekID = $row['weekID'];
}
$yearResult = mysql_query("SELECT year FROM year WHERE currentYear='true'", $connection) or die('Query failed.');
if ($row = mysql_fetch_assoc($yearResult)) {
    $year = $row['year'];
}

// Load the logged-in user's picks for this week (to highlight their chosen teams)
$userPicks = array();
if (isset($_SESSION['username'])) {
    $username = $_SESSION['username'];
    $memberResult = mysql_query("SELECT memberid FROM member WHERE username='$username' LIMIT 1", $connection) or die('Query failed.');
    if ($row = mysql_fetch_assoc($memberResult)) {
        $memberId = $row['memberid'];
        mysql_free_result($memberResult);
        $pickQuery = "SELECT tl.id as logoId, t.teamname as teamName
                      FROM pick p
                      JOIN team t ON p.teamID = t.teamID
                      LEFT JOIN team_logo tl ON BINARY t.teamname = BINARY tl.team
                      WHERE p.memberID='$memberId' AND p.weekID='$weekID' AND p.yearID='$year'";
        $pickResult = mysql_query($pickQuery, $connection) or die('Query failed.');
        while ($row = mysql_fetch_assoc($pickResult)) {
            $logoId  = isset($row['logoId'])  ? (string)trim($row['logoId'])    : '';
            $teamName= isset($row['teamName'])? trim($row['teamName'])          : '';
            if ($logoId !== '') {
                $userPicks['id:' . $logoId] = true;
            }
            if ($teamName !== '') {
                $userPicks['name:' . $teamName] = true;
            }
        }
        mysql_free_result($pickResult);
    } else {
        mysql_free_result($memberResult);
    }
}

// Identify AFPLNA “Games of the Week” (to mark them specially)
$afplnaGames = array();
$gamesQuery = "SELECT tlh.id as homeId, tla.id as awayId 
               FROM game g 
               JOIN team th ON g.homeID = th.teamID 
               JOIN team ta ON g.awayID = ta.teamID 
               LEFT JOIN team_logo tlh ON BINARY th.teamname = BINARY tlh.team
               LEFT JOIN team_logo tla ON BINARY ta.teamname = BINARY tla.team
               WHERE g.weekID='$weekID' AND g.yearID='$year'";
$gamesResult = mysql_query($gamesQuery, $connection) or die('Query failed.');
while ($row = mysql_fetch_assoc($gamesResult)) {
    $homeId = isset($row['homeId']) ? (string)$row['homeId'] : '';
    $awayId = isset($row['awayId']) ? (string)$row['awayId'] : '';
    if ($homeId && $awayId) {
        $afplnaGames[$homeId . '|' . $awayId] = true;
    }
}
mysql_free_result($gamesResult);

// Load team logos and names for mapping team IDs to names
$teamData = array();
$teamNameToId = array();
$teamResult = mysql_query(
    "SELECT tl.id, tl.url, t.teamname
     FROM team_logo tl
     JOIN team t ON BINARY tl.team = BINARY t.teamname",
    $connection
) or die('Query failed.');
while ($row = mysql_fetch_assoc($teamResult)) {
    $id       = (string)trim($row['id']);
    $teamName = trim($row['teamname']);

    // Primary lookup by numeric ID
    $teamData[$id] = array(
        'id'   => $id,
        'logo' => trim($row['url']),
        'name' => $teamName
    );

    // Secondary lookup by exact school name for fallback matches
    $teamNameToId[$teamName] = $id;
}
mysql_free_result($teamResult);

// Retrieve API key for external CollegeFootballData API (for live scores)
$apiKey = '';
$keyResult = mysql_query("SELECT `KEY` FROM API_KEYS WHERE API_NAME='CFD' LIMIT 1", $connection);
if ($keyResult && $row = mysql_fetch_assoc($keyResult)) {
    $apiKey = trim($row['KEY']);
    mysql_free_result($keyResult);
}
if (!$apiKey) {
    // fallback to constants or env if not found in DB
    if (defined('CFBD_API_KEY') && CFBD_API_KEY) {
        $apiKey = CFBD_API_KEY;
    } elseif (!empty($CFBD_API_KEY)) {
        $apiKey = $CFBD_API_KEY;
    } else {
        $apiKey = getenv('CFBD_API_KEY');
    }
}

// Retrieve Google API key (if used for ads or other services)
$googleApiKey = '';
$googleResult = mysql_query("SELECT `KEY` FROM API_KEYS WHERE API_NAME='google' LIMIT 1", $connection);
if ($googleResult && $row = mysql_fetch_assoc($googleResult)) {
    $googleApiKey = trim($row['KEY']);
    mysql_free_result($googleResult);
}

// ** AFPLNA API Base URL and Key ** 
$AFPLNA_API_BASE = 'http://143.198.20.72';  // DigitalOcean droplet base (HTTP)
$AFPLNA_API_KEY  = '';
$afplnaKeyResult = mysql_query("SELECT `KEY` FROM API_KEYS WHERE API_NAME='cfbmatchupreport' LIMIT 1", $connection);
if ($afplnaKeyResult && $row = mysql_fetch_assoc($afplnaKeyResult)) {
    $AFPLNA_API_KEY = trim($row['KEY']);
    mysql_free_result($afplnaKeyResult);
}

// Fetch live FBS scoreboard data for the current AFPLNA week/year
$url = "https://api.collegefootballdata.com/scoreboard?classification=fbs"
     . "&year=" . urlencode($year)
     . "&week=" . urlencode($weekID);
$ch  = curl_init($url);
curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
$headers = array("Accept: application/json");
if ($apiKey) {
    $headers[] = "Authorization: Bearer $apiKey";
}
curl_setopt($ch, CURLOPT_HTTPHEADER, $headers);
$response = curl_exec($ch);
$httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
curl_close($ch);
$data = array();
if ($httpCode === 200) {
    $tmp = json_decode($response, true);
    if (json_last_error() === JSON_ERROR_NONE && is_array($tmp)) {
        $data = $tmp;
    }
}

// Separate out featured (AFPLNA) games vs other games
$featuredGames = array();
$otherGames    = array();
foreach ($data as $game) {
    $homeName   = isset($game['homeTeam']['name'])   ? $game['homeTeam']['name']   : '';
    $awayName   = isset($game['awayTeam']['name'])   ? $game['awayTeam']['name']   : '';
    $homeSchool = isset($game['homeTeam']['school']) ? $game['homeTeam']['school'] : '';
    $awaySchool = isset($game['awayTeam']['school']) ? $game['awayTeam']['school'] : '';
    $homeId     = isset($game['homeTeam']['id'])     ? (string)$game['homeTeam']['id']     : '';
    $awayId     = isset($game['awayTeam']['id'])     ? (string)$game['awayTeam']['id']     : '';

    $homeKey = trim($homeSchool);
    $awayKey = trim($awaySchool);

    // Resolve IDs via team name if missing or mismatched using exact matches
    if ((!$homeId || !isset($teamData[$homeId])) && $homeKey && isset($teamNameToId[$homeKey])) {
        $homeId = $teamNameToId[$homeKey];
    }
    if ((!$awayId || !isset($teamData[$awayId])) && $awayKey && isset($teamNameToId[$awayKey])) {
        $awayId = $teamNameToId[$awayKey];
    }

    $key       = $homeId . '|' . $awayId;
    $isAfplna  = isset($afplnaGames[$key]);

    // Determine if the logged-in user picked one of these teams.
    // Only display the pick when the matchup is an AFPLNA game (Game of the Week).
    $yourPick = '';
    if ($isAfplna) {
        if (isset($userPicks['id:' . $homeId]) || ($homeKey && isset($userPicks['name:' . $homeKey]))) {
            $yourPick = isset($teamData[$homeId]['name']) ? $teamData[$homeId]['name'] : ($homeSchool ?: $homeName);
        } elseif (isset($userPicks['id:' . $awayId]) || ($awayKey && isset($userPicks['name:' . $awayKey]))) {
            $yourPick = isset($teamData[$awayId]['name']) ? $teamData[$awayId]['name'] : ($awaySchool ?: $awayName);
        }
    }

    $info = array(
        'home'       => isset($teamData[$homeId]['name']) ? $teamData[$homeId]['name'] : ($homeSchool ?: $homeName),
        'away'       => isset($teamData[$awayId]['name']) ? $teamData[$awayId]['name'] : ($awaySchool ?: $awayName),
        'homeDbName' => isset($teamData[$homeId]['name']) ? $teamData[$homeId]['name'] : ($homeSchool ?: $homeName),
        'awayDbName' => isset($teamData[$awayId]['name']) ? $teamData[$awayId]['name'] : ($awaySchool ?: $awayName),
        'homeLogo'   => isset($teamData[$homeId]['logo']) ? $teamData[$homeId]['logo'] : '',
        'awayLogo'   => isset($teamData[$awayId]['logo']) ? $teamData[$awayId]['logo'] : '',
        'venue'      => isset($game['venue']['name']) ? $game['venue']['name'] : '',
        'start'      => isset($game['startDate']) ? date('n/j g:i A', strtotime($game['startDate'])) : '',
        'tv'         => isset($game['tv']) ? $game['tv'] : '',
        'status'     => isset($game['status']) ? $game['status'] : '',
        'period'     => isset($game['period']) ? $game['period'] : '',
        'clock'      => isset($game['clock']) ? $game['clock'] : '',
        'situation'  => isset($game['situation']) ? $game['situation'] : '',
        'possession' => isset($game['possession']) ? $game['possession'] : '',
        'lastPlay'   => isset($game['lastPlay']) ? $game['lastPlay'] : '',
        'homePoints' => isset($game['homeTeam']['points']) ? $game['homeTeam']['points'] : '',
        'awayPoints' => isset($game['awayTeam']['points']) ? $game['awayTeam']['points'] : '',
        'windDir'    => isset($game['weather']['windDirection']) ? $game['weather']['windDirection'] : '',
        'windSpeed'  => isset($game['weather']['windSpeed']) ? $game['weather']['windSpeed'] : '',
        'weatherDesc'=> isset($game['weather']['description']) ? $game['weather']['description'] : '',
        'temperature'=> isset($game['weather']['temperature']) ? $game['weather']['temperature'] : '',
        'awayML'     => isset($game['betting']['awayMoneyline']) ? $game['betting']['awayMoneyline'] : '',
        'homeML'     => isset($game['betting']['homeMoneyline']) ? $game['betting']['homeMoneyline'] : '',
        'overUnder'  => isset($game['betting']['overUnder']) ? $game['betting']['overUnder'] : '',
        'spread'     => isset($game['betting']['spread']) ? $game['betting']['spread'] : '',
        'yourPick'   => $yourPick
    );

    if ($isAfplna) {
        $info['afplna'] = true;
        $featuredGames[] = $info;
    } else {
        $info['afplna'] = false;
        $otherGames[] = $info;
    }
}
mysql_close($connection);
?>
<html>
<head>
    <title>Scoreboard</title>
    <style>
        body { font-family: Arial, sans-serif; background-image: url('yellow_weave.gif'); }
        .scoreboard { max-width: 1000px; margin: 0 auto; }
        .game { border-radius: 8px; overflow: hidden; margin: 20px 0; 
                box-shadow: 0 2px 6px rgba(0,0,0,0.15); background: #fff; }
        .game.afplna { border: 2px solid gold; }
        .score-header { display: flex; justify-content: space-between; align-items: center;
                        background: #003366; color: #fff; padding: 10px; font-size: 18px; font-weight: bold; }
        .score-header .team-name { flex: 1; text-align: center; }
        .score-header .score { font-size: 24px; min-width: 100px; text-align: center; }
        .game-details { padding: 10px; background: #f9f9f9; font-size: 14px; line-height: 1.4; }
        .game-details div { margin: 4px 0; }
        /* Highlight the user's pick without stretching across the row */
        .your-pick {
            background-color: yellow;
            display: inline-block;
            padding: 0 4px;
        }
        .section-title { background: #003366; color: white; padding: 5px; margin-top: 20px; }
        .refresh { margin-bottom: 15px; }
        .team-logo { width: 24px; height: 24px; object-fit: contain; vertical-align: middle; margin-right: 5px; }
        /* AI report progress UI */
        .ai-progress { display: none; margin-top: 8px; max-width: 520px; }
        .ai-progress.show { display: block; }
        .ai-bar { height: 10px; background: #e3e6ea; border-radius: 5px; overflow: hidden; }
        .ai-bar > span { display: block; height: 100%; width: 0%; background: #003366;
                         transition: width .5s ease; }
        .ai-progress.err .ai-bar > span { background: #c00; }
        .ai-phase { font-size: 12px; color: #444; margin-top: 4px; }
        .ai-phase .ai-elapsed { color: #777; }
        .ai-detail { font-size: 11px; color: #c00; margin-top: 3px; word-break: break-word; }
        .ai-controls button[disabled] { opacity: .55; cursor: not-allowed; }
    </style>
</head>
<body>
<div class="scoreboard">
    <center><img src="afplnalogo.gif" alt="AFPLNA Logo"></center>
    <h1>FBS Scoreboard</h1>
    <form method="post" class="refresh">
        <input type="submit" value="Refresh Scores">
        <button type="button" onclick="window.location.href='index.php';">Home</button>
    </form>
    <?php
    if (!empty($featuredGames)) {
        echo "<h2 class='section-title'>AFPLNA Games of the Week</h2>";
        foreach ($featuredGames as $g) {
            echo "<div class='game afplna'>";
            echo "<div class='score-header'>";
            // Away team
            echo "<div class='team-name'>";
            if (!empty($g['awayLogo'])) {
                echo "<img src='" . htmlspecialchars($g['awayLogo']) . "' class='team-logo'>";
            }
            echo htmlspecialchars($g['away']) . "</div>";
            // Score
            echo "<div class='score'>" . htmlspecialchars($g['awayPoints']) . " @ " . htmlspecialchars($g['homePoints']) . "</div>";
            // Home team
            echo "<div class='team-name'>";
            if (!empty($g['homeLogo'])) {
                echo "<img src='" . htmlspecialchars($g['homeLogo']) . "' class='team-logo'>";
            }
            echo htmlspecialchars($g['home']) . "</div>";
            echo "</div>";  // .score-header

            echo "<div class='game-details'>";
            echo "<div><b>Venue:</b> " . htmlspecialchars($g['venue']) . " | <b>Start:</b> " . htmlspecialchars($g['start']) . " | <b>TV:</b> " . htmlspecialchars($g['tv']) . "</div>";
            echo "<div><b>Status:</b> " . htmlspecialchars($g['status']) . " | <b>Period:</b> " . htmlspecialchars($g['period']) . " | <b>Clock:</b> " . htmlspecialchars($g['clock']) . "</div>";
            echo "<div><b>Situation:</b> " . htmlspecialchars($g['situation']) . " | <b>Possession:</b> " . htmlspecialchars($g['possession']) . "</div>";
            echo "<div><b>Last Play:</b> " . htmlspecialchars($g['lastPlay']) . "</div>";
            echo "<div><b>Weather:</b> " . htmlspecialchars($g['weatherDesc']) . ", Temp " . htmlspecialchars($g['temperature']) . "°, Wind " . htmlspecialchars($g['windDir']) . "° @ " . htmlspecialchars($g['windSpeed']) . " mph</div>";
            echo "<div><b>Lines:</b> Away ML " . htmlspecialchars($g['awayML']) . ", Home ML " . htmlspecialchars($g['homeML']) . ", O/U " . htmlspecialchars($g['overUnder']) . ", Spread " . htmlspecialchars($g['spread']) . "</div>";
            if (!empty($g['yourPick'])) {
                echo "<div><span class='your-pick'><b>Your Pick:</b> " . htmlspecialchars($g['yourPick']) . "</span></div>";
            }
            // AI Report controls
            echo '<div class="ai-controls" style="margin:12px 0;">';
            echo '<button type="button" class="btn-generate" '
                 . 'data-homefull="' . htmlspecialchars($g['home'], ENT_QUOTES) . '" '
                 . 'data-awayfull="' . htmlspecialchars($g['away'], ENT_QUOTES) . '" '
                 . 'data-homeshort="' . htmlspecialchars($g['homeDbName'], ENT_QUOTES) . '" '
                 . 'data-awayshort="' . htmlspecialchars($g['awayDbName'], ENT_QUOTES) . '">'
                 . 'Generate AI Report</button> ';
            echo '<button type="button" class="btn-download">Download AI Report</button>';
            echo '<span class="ai-status" style="margin-left:10px;color:#0a0;">&nbsp;</span>';
            echo '</div>';  // .ai-controls

            echo "</div>";  // .game-details
            echo "</div>";  // .game
        }
    }
    if (!empty($otherGames)) {
        echo "<h2 class='section-title'>All FBS Games</h2>";
        foreach ($otherGames as $g) {
            echo "<div class='game'>";
            echo "<div class='score-header'>";
            // Away team
            echo "<div class='team-name'>";
            if (!empty($g['awayLogo'])) {
                echo "<img src='" . htmlspecialchars($g['awayLogo']) . "' class='team-logo'>";
            }
            echo htmlspecialchars($g['away']) . "</div>";
            // Score
            echo "<div class='score'>" . htmlspecialchars($g['awayPoints']) . " @ " . htmlspecialchars($g['homePoints']) . "</div>";
            // Home team
            echo "<div class='team-name'>";
            if (!empty($g['homeLogo'])) {
                echo "<img src='" . htmlspecialchars($g['homeLogo']) . "' class='team-logo'>";
            }
            echo htmlspecialchars($g['home']) . "</div>";
            echo "</div>";

            echo "<div class='game-details'>";
            echo "<div><b>Venue:</b> " . htmlspecialchars($g['venue']) . " | <b>Start:</b> " . htmlspecialchars($g['start']) . " | <b>TV:</b> " . htmlspecialchars($g['tv']) . "</div>";
            echo "<div><b>Status:</b> " . htmlspecialchars($g['status']) . " | <b>Period:</b> " . htmlspecialchars($g['period']) . " | <b>Clock:</b> " . htmlspecialchars($g['clock']) . "</div>";
            echo "<div><b>Situation:</b> " . htmlspecialchars($g['situation']) . " | <b>Possession:</b> " . htmlspecialchars($g['possession']) . "</div>";
            echo "<div><b>Last Play:</b> " . htmlspecialchars($g['lastPlay']) . "</div>";
            echo "<div><b>Weather:</b> " . htmlspecialchars($g['weatherDesc']) . ", Temp " . htmlspecialchars($g['temperature']) . "°, Wind " . htmlspecialchars($g['windDir']) . "° @ " . htmlspecialchars($g['windSpeed']) . " mph</div>";
            echo "<div><b>Lines:</b> Away ML " . htmlspecialchars($g['awayML']) . ", Home ML " . htmlspecialchars($g['homeML']) . ", O/U " . htmlspecialchars($g['overUnder']) . ", Spread " . htmlspecialchars($g['spread']) . "</div>";
            if (!empty($g['yourPick'])) {
                echo "<div><span class='your-pick'><b>Your Pick:</b> " . htmlspecialchars($g['yourPick']) . "</span></div>";
            }
            // AI Report controls (for completeness, allow reports on any game)
            echo '<div class="ai-controls" style="margin:12px 0;">';
            echo '<button type="button" class="btn-generate" '
                 . 'data-homefull="' . htmlspecialchars($g['home'], ENT_QUOTES) . '" '
                 . 'data-awayfull="' . htmlspecialchars($g['away'], ENT_QUOTES) . '" '
                 . 'data-homeshort="' . htmlspecialchars($g['homeDbName'], ENT_QUOTES) . '" '
                 . 'data-awayshort="' . htmlspecialchars($g['awayDbName'], ENT_QUOTES) . '">'
                 . 'Generate AI Report</button> ';
            echo '<button type="button" class="btn-download">Download AI Report</button>';
            echo '<span class="ai-status" style="margin-left:10px;color:#0a0;">&nbsp;</span>';
            echo '</div>';

            echo "</div>";
            echo "</div>";
        }
    }
    ?>
</div>
<script>
// Embed API base URL and key from PHP into JavaScript constants
const API_BASE = "<?= $AFPLNA_API_BASE ?>";
const API_KEY  = "<?= $AFPLNA_API_KEY ?>";

// Report generation is asynchronous: POST /generate-report returns 202 immediately and
// the real progress comes from polling /report-status. Never hold the POST open — it
// runs for minutes and any proxy in between will cut the connection first.
const POLL_MS      = 4000;
const MAX_WAIT_MS  = 15 * 60 * 1000;   // give up watching after 15 minutes

// fetch() reports CORS, mixed-content and DNS failures identically, as an opaque
// TypeError. Turn each into something that names the actual problem.
function describeFetchError(err) {
  if (err && err.status === 401) {
    return 'API key rejected (HTTP 401). The key this page sends must match SERVICE_API_KEY '
         + 'on the report server.';
  }
  if (err && err.status) return err.message;
  if (err && err.name === 'TypeError') {
    if (location.protocol === 'https:' && /^http:/i.test(API_BASE)) {
      return 'Blocked as mixed content: this page is HTTPS but the API is plain HTTP ('
           + API_BASE + '). Browsers refuse that. Put the API behind HTTPS.';
    }
    return 'Could not reach ' + API_BASE + ' — network, DNS or CORS. Open DevTools > Network '
         + 'for the exact failure.';
  }
  return (err && err.message) ? err.message : String(err);
}

function fmtElapsed(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const m = Math.floor(sec / 60), s = sec % 60;
  return m + ':' + String(s).padStart(2, '0');
}

window.addEventListener('DOMContentLoaded', () => {
  // Config problems are cheap to detect and expensive to guess at. Log once, up front.
  if (!API_KEY) {
    console.error('AFPLNA: API_KEY is empty. The cfbmatchupreport row in API_KEYS did not load, '
                + 'so every request will come back 401.');
  }
  if (location.protocol === 'https:' && /^http:/i.test(API_BASE)) {
    console.error('AFPLNA: page is HTTPS but API_BASE is HTTP (' + API_BASE
                + '). The browser will block every request as mixed content.');
  }
  console.info('AFPLNA report client: API_BASE=' + API_BASE
             + ' key=' + (API_KEY ? API_KEY.slice(0, 4) + '…(' + API_KEY.length + ' chars)' : 'MISSING'));

  document.querySelectorAll('.ai-controls').forEach(ctrl => {
    const $gen = ctrl.querySelector('.btn-generate');
    const $dl  = ctrl.querySelector('.btn-download');
    const $st  = ctrl.querySelector('.ai-status');

    // Progress UI is built here rather than in PHP so the markup lives in one place.
    const $prog = document.createElement('div');
    $prog.className = 'ai-progress';
    $prog.innerHTML = '<div class="ai-bar"><span></span></div>'
                    + '<div class="ai-phase"><span class="ai-phase-text"></span> '
                    + '<span class="ai-elapsed"></span></div>'
                    + '<div class="ai-detail"></div>';
    ctrl.appendChild($prog);
    const $bar    = $prog.querySelector('.ai-bar > span');
    const $phase  = $prog.querySelector('.ai-phase-text');
    const $elapsed= $prog.querySelector('.ai-elapsed');
    const $detail = $prog.querySelector('.ai-detail');

    const home_short = $gen.dataset.homeshort;
    const away_short = $gen.dataset.awayshort;

    let pollTimer = null;
    let watchStarted = 0;

    function setStatus(msg, isErr = false) {
      $st.textContent = msg;
      $st.style.color = isErr ? '#c00' : '#0a0';
      $st.style.backgroundColor = (!isErr && msg) ? '#cfc' : 'transparent';
      $st.style.padding = (!isErr && msg) ? '2px 4px' : '0';
    }

    function showProgress(percent, phase, elapsedSec, isErr = false, detail = '') {
      $prog.classList.add('show');
      $prog.classList.toggle('err', !!isErr);
      $bar.style.width = Math.max(2, Math.min(100, percent || 0)) + '%';
      $phase.textContent = phase || '';
      $elapsed.textContent = elapsedSec != null ? '(' + fmtElapsed(elapsedSec) + ')' : '';
      $detail.textContent = detail || '';
    }

    function hideProgress() {
      $prog.classList.remove('show', 'err');
      $detail.textContent = '';
    }

    function setBusy(busy) {
      $gen.disabled = busy;
      $gen.textContent = busy ? 'Generating…'
                              : ($dl.dataset.ready === '1' ? 'Regenerate AI Report'
                                                           : 'Generate AI Report');
    }

    async function fetchStatus() {
      const url = `${API_BASE}/report-status?api_key=${encodeURIComponent(API_KEY)}`
                + `&home_team=${encodeURIComponent(home_short)}`
                + `&away_team=${encodeURIComponent(away_short)}&_=${Date.now()}`;
      const resp = await fetch(url, { cache: 'no-store' });
      if (!resp.ok) {
        let detail = '';
        try {
          const j = await resp.json();
          detail = j.error || '';
        } catch (e) {
          detail = (await resp.text().catch(() => '')).slice(0, 120);
        }
        const err = new Error(`HTTP ${resp.status}${detail ? ': ' + detail : ''}`);
        err.status = resp.status;
        throw err;
      }
      return resp.json();
    }

    // Single source of truth: render whatever the server says the job is doing.
    function applyStatus(data) {
      const exists = !!(data && data.report_exists);
      $dl.dataset.ready = exists ? '1' : '0';
      $dl.title = exists ? 'Download AI Report' : 'Report not yet Generated.';

      const state = (data && data.state) || 'none';

      if (state === 'queued' || state === 'running') {
        setBusy(true);
        setStatus('Generating the AI report — this takes a few minutes.');
        showProgress(data.percent, data.message, data.elapsed_seconds);
        return 'busy';
      }

      setBusy(false);

      if (state === 'error') {
        setStatus(data.error || 'Report generation failed.', true);
        showProgress(100, data.error || 'Failed', data.elapsed_seconds, true, data.detail || '');
        return 'error';
      }

      if (state === 'done') {
        const r = data.result || {};
        let msg = 'Report is ready ✔';
        if (r.seconds) msg += ` (${fmtElapsed(r.seconds)}, ${r.sources || 0} sources)`;
        setStatus(msg);
        hideProgress();
        return 'done';
      }

      // state === 'none' — nothing queued in this server process
      hideProgress();
      if (exists) setStatus('Report is ready ✔');
      else setStatus('Report not yet Generated.', true);
      return exists ? 'done' : 'idle';
    }

    function stopPolling() {
      if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    }

    function startPolling() {
      stopPolling();
      watchStarted = watchStarted || Date.now();
      const tick = async () => {
        try {
          const data = await fetchStatus();
          const outcome = applyStatus(data);
          if (outcome !== 'busy') { watchStarted = 0; return; }
        } catch (err) {
          console.error('Error polling report status:', err);
          // A transient network blip must not abandon a job that is still running.
        }
        if (Date.now() - watchStarted > MAX_WAIT_MS) {
          setBusy(false);
          setStatus('Still generating — check back shortly or refresh the page.', true);
          hideProgress();
          watchStarted = 0;
          return;
        }
        pollTimer = setTimeout(tick, POLL_MS);
      };
      pollTimer = setTimeout(tick, POLL_MS);
    }

    async function generateReport() {
      if ($dl.dataset.ready === '1') {
        if (!confirm('A report is already available for this game. Do you want to generate a new updated report?')) {
          return;
        }
      }

      setBusy(true);
      setStatus('Starting the AI report…');
      showProgress(2, 'Queued', 0);

      try {
        const resp = await fetch(`${API_BASE}/generate-report`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            api_key: API_KEY,
            home_full:  $gen.dataset.homefull,
            away_full:  $gen.dataset.awayfull,
            home_short: home_short,
            away_short: away_short
          })
        });

        // 202 Accepted is the normal path; the job now runs server-side.
        if (!resp.ok && resp.status !== 202) {
          let errMsg = `Error starting report (HTTP ${resp.status})`;
          try {
            const errData = await resp.json();
            if (errData.error) errMsg = `${errData.error} (HTTP ${resp.status})`;
            if (errData.detail) errMsg += ` — ${errData.detail}`;
          } catch (e) {
            const body = await resp.text().catch(() => '');
            if (body) errMsg += `: ${body.slice(0, 160)}`;
          }
          if (resp.status === 401) {
            errMsg = 'API key rejected (HTTP 401). The key this page sends must match '
                   + 'SERVICE_API_KEY on the report server.';
          }
          setBusy(false);
          setStatus(errMsg, true);
          hideProgress();
          return;
        }

        const job = await resp.json().catch(() => ({}));
        showProgress(job.percent || 2, job.message || 'Queued', 0);
      } catch (err) {
        console.error('Failed to start report generation:', err);
        setBusy(false);
        setStatus(describeFetchError(err), true);
        hideProgress();
        return;
      }

      watchStarted = Date.now();
      startPolling();
    }

    async function downloadReport() {
      if ($dl.dataset.ready !== '1') {
        try { applyStatus(await fetchStatus()); } catch (e) { /* fall through */ }
      }
      if ($dl.dataset.ready === '1') {
        window.location.href = `${API_BASE}/get-report?api_key=${encodeURIComponent(API_KEY)}`
                             + `&home_team=${encodeURIComponent(home_short)}`
                             + `&away_team=${encodeURIComponent(away_short)}&_=${Date.now()}`;
      } else {
        setStatus('A report is not available, please run the AI report generation for this matchup.', true);
      }
    }

    // On load, adopt whatever the server is already doing — a build kicked off in
    // another tab (or before a refresh) keeps showing live progress here.
    (async () => {
      try {
        const data = await fetchStatus();
        if (applyStatus(data) === 'busy') {
          watchStarted = Date.now() - (data.elapsed_seconds || 0) * 1000;
          startPolling();
        }
      } catch (err) {
        console.error('Report status check failed:', err);
        setStatus(describeFetchError(err), true);
      }
    })();

    $gen.addEventListener('click', generateReport);
    $dl.addEventListener('click', downloadReport);
  });
});
</script>
</body>
</html>
