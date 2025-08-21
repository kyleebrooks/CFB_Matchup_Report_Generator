<?php
include 'common.inc';
if (!SessionStarted())
    $ses = 'true';
else
    $ses = 'false';
require_once 'api_keys.php';

// Connect to the database using MySQLi (mysql_* functions are deprecated/removed)
$connection = mysqli_connect(
    "p3nlmysql149plsk.secureserver.net",
    "kdogg4207",
    "xMkM2941",
    "kdogg4207"
);
if (!$connection) {
    die("Could not connect to database server");
}

// Get current week and year
$weekID = null;
$year   = null;
$weekResult = mysqli_query($connection, "SELECT weekID FROM week WHERE currentWeek='true'") or die('Query failed.');
if ($row = mysqli_fetch_assoc($weekResult)) {
    $weekID = $row['weekID'];
}
$yearResult = mysqli_query($connection, "SELECT year FROM year WHERE currentYear='true'") or die('Query failed.');
if ($row = mysqli_fetch_assoc($yearResult)) {
    $year = $row['year'];
}

// Load the logged-in user's picks for this week (to highlight their chosen teams)
$userPicks = array();
if (isset($_SESSION['username'])) {
    $username = $_SESSION['username'];
    $memberResult = mysqli_query($connection, "SELECT memberid FROM member WHERE username='$username' LIMIT 1") or die('Query failed.');
    if ($row = mysqli_fetch_assoc($memberResult)) {
        $memberId = $row['memberid'];
        mysqli_free_result($memberResult);
        $pickQuery = "SELECT tl.id as logoId, LOWER(TRIM(t.teamname)) as teamName 
                      FROM pick p 
                      JOIN team t ON p.teamID = t.teamID 
                      LEFT JOIN team_logo tl ON LOWER(TRIM(t.teamname)) = LOWER(TRIM(tl.team)) 
                      WHERE p.memberID='$memberId' AND p.weekID='$weekID' AND p.yearID='$year'";
        $pickResult = mysqli_query($connection, $pickQuery) or die('Query failed.');
        while ($row = mysqli_fetch_assoc($pickResult)) {
            $logoId  = isset($row['logoId'])  ? (string)trim($row['logoId'])    : '';
            $teamName= isset($row['teamName'])? trim($row['teamName'])          : '';
            if ($logoId !== '') {
                $userPicks['id:' . $logoId] = true;
            }
            if ($teamName !== '') {
                $userPicks['name:' . $teamName] = true;
            }
        }
        mysqli_free_result($pickResult);
    } else {
        mysqli_free_result($memberResult);
    }
}

// Identify AFPLNA “Games of the Week” (to mark them specially)
$afplnaGames = array();
$gamesQuery = "SELECT tlh.id as homeId, tla.id as awayId 
               FROM game g 
               JOIN team th ON g.homeID = th.teamID 
               JOIN team ta ON g.awayID = ta.teamID 
               LEFT JOIN team_logo tlh ON LOWER(TRIM(th.teamname)) = LOWER(TRIM(tlh.team))
               LEFT JOIN team_logo tla ON LOWER(TRIM(ta.teamname)) = LOWER(TRIM(tla.team))
               WHERE g.weekID='$weekID' AND g.yearID='$year'";
$gamesResult = mysqli_query($connection, $gamesQuery) or die('Query failed.');
while ($row = mysqli_fetch_assoc($gamesResult)) {
    $homeId = isset($row['homeId']) ? (string)$row['homeId'] : '';
    $awayId = isset($row['awayId']) ? (string)$row['awayId'] : '';
    if ($homeId && $awayId) {
        $afplnaGames[$homeId . '|' . $awayId] = true;
    }
}
mysqli_free_result($gamesResult);

// Load team logos and names for mapping team IDs to names
$teamData = array();
$teamResult = mysqli_query(
    $connection,
    "SELECT tl.id, tl.url, t.teamname
     FROM team_logo tl
     JOIN team t ON LOWER(TRIM(tl.team)) = LOWER(TRIM(t.teamname))"
) or die('Query failed.');
while ($row = mysqli_fetch_assoc($teamResult)) {
    $id = (string)trim($row['id']);
    $teamData[$id] = array(
        'logo' => trim($row['url']),
        'name' => trim($row['teamname'])
    );
}
mysqli_free_result($teamResult);

// Retrieve API key for external CollegeFootballData API (for live scores)
$apiKey = '';
$keyResult = mysqli_query($connection, "SELECT `KEY` FROM API_KEYS WHERE API_NAME='CFD' LIMIT 1");
if ($keyResult && $row = mysqli_fetch_assoc($keyResult)) {
    $apiKey = trim($row['KEY']);
    mysqli_free_result($keyResult);
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
$googleResult = mysqli_query($connection, "SELECT `KEY` FROM API_KEYS WHERE API_NAME='google' LIMIT 1");
if ($googleResult && $row = mysqli_fetch_assoc($googleResult)) {
    $googleApiKey = trim($row['KEY']);
    mysqli_free_result($googleResult);
}

// ** AFPLNA API Base URL and Key ** 
$AFPLNA_API_BASE = 'http://143.198.20.72';  // DigitalOcean droplet base (HTTP)
$AFPLNA_API_KEY  = '';
$afplnaKeyResult = mysqli_query($connection, "SELECT `KEY` FROM API_KEYS WHERE API_NAME='cfbmatchupreport' LIMIT 1");
if ($afplnaKeyResult && $row = mysqli_fetch_assoc($afplnaKeyResult)) {
    $AFPLNA_API_KEY = trim($row['KEY']);
    mysqli_free_result($afplnaKeyResult);
}

// Fetch live FBS scoreboard data (current games and scores)
$url = "https://api.collegefootballdata.com/scoreboard?classification=fbs";
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
    $homeName = isset($game['homeTeam']['name']) ? $game['homeTeam']['name'] : '';
    $awayName = isset($game['awayTeam']['name']) ? $game['awayTeam']['name'] : '';
    $homeId   = isset($game['homeTeam']['id']) ? (string)$game['homeTeam']['id'] : '';
    $awayId   = isset($game['awayTeam']['id']) ? (string)$game['awayTeam']['id'] : '';
    $key      = $homeId . '|' . $awayId;
    // Determine if the logged-in user picked one of these teams
    $yourPick = '';
    $homeNameNorm = strtolower(trim($homeName));
    $awayNameNorm = strtolower(trim($awayName));
    if (isset($userPicks['id:' . $homeId]) || isset($userPicks['name:' . $homeNameNorm])) {
        $yourPick = $homeName;
    } elseif (isset($userPicks['id:' . $awayId]) || isset($userPicks['name:' . $awayNameNorm])) {
        $yourPick = $awayName;
    }
    $info = array(
        'home'       => $homeName,
        'away'       => $awayName,
        'homeDbName' => isset($teamData[$homeId]['name']) ? $teamData[$homeId]['name'] : $homeName,  // normalized name for consistency
        'awayDbName' => isset($teamData[$awayId]['name']) ? $teamData[$awayId]['name'] : $awayName,
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
    if (isset($afplnaGames[$key])) {
        $info['afplna'] = true;
        $featuredGames[] = $info;
    } else {
        $info['afplna'] = false;
        $otherGames[] = $info;
    }
}
mysqli_close($connection);
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
        .section-title { background: #003366; color: white; padding: 5px; margin-top: 20px; }
        .refresh { margin-bottom: 15px; }
        .team-logo { width: 24px; height: 24px; object-fit: contain; vertical-align: middle; margin-right: 5px; }
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
                echo "<div><b>Your Pick:</b> " . htmlspecialchars($g['yourPick']) . "</div>";
            }
            // AI Report controls
            echo '<div class="ai-controls" style="margin:12px 0;">';
            echo '<button type="button" class="btn-generate" '
                 . 'data-homefull="' . htmlspecialchars($g['home'], ENT_QUOTES) . '" '
                 . 'data-awayfull="' . htmlspecialchars($g['away'], ENT_QUOTES) . '" '
                 . 'data-homeshort="' . htmlspecialchars($g['homeDbName'], ENT_QUOTES) . '" '
                 . 'data-awayshort="' . htmlspecialchars($g['awayDbName'], ENT_QUOTES) . '">'
                 . 'Generate AI Report</button> ';
            echo '<button type="button" class="btn-check">Check AI Report</button> ';
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
                echo "<div><b>Your Pick:</b> " . htmlspecialchars($g['yourPick']) . "</div>";
            }
            // AI Report controls (for completeness, allow reports on any game)
            echo '<div class="ai-controls" style="margin:12px 0;">';
            echo '<button type="button" class="btn-generate" '
                 . 'data-homefull="' . htmlspecialchars($g['home'], ENT_QUOTES) . '" '
                 . 'data-awayfull="' . htmlspecialchars($g['away'], ENT_QUOTES) . '" '
                 . 'data-homeshort="' . htmlspecialchars($g['homeDbName'], ENT_QUOTES) . '" '
                 . 'data-awayshort="' . htmlspecialchars($g['awayDbName'], ENT_QUOTES) . '">'
                 . 'Generate AI Report</button> ';
            echo '<button type="button" class="btn-check">Check AI Report</button> ';
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

window.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.ai-controls').forEach(ctrl => {
    const $gen = ctrl.querySelector('.btn-generate');
    const $chk = ctrl.querySelector('.btn-check');
    const $dl  = ctrl.querySelector('.btn-download');
    const $st  = ctrl.querySelector('.ai-status');

    function setStatus(msg, isErr=false) {
      $st.textContent = msg;
      $st.style.color = isErr ? '#c00' : '#0a0';
      $st.style.backgroundColor = (!isErr && msg) ? '#cfc' : 'transparent';
      $st.style.padding = (!isErr && msg) ? '2px 4px' : '0';
    }

    // Check if a report PDF exists on the server for this matchup
    async function checkReportExists(showStatus = false) {
      const home_short = $gen.dataset.homeshort;
      const away_short = $gen.dataset.awayshort;
      try {
        const url = `${API_BASE}/has-report?api_key=${encodeURIComponent(API_KEY)}&home_team=${encodeURIComponent(home_short)}&away_team=${encodeURIComponent(away_short)}&_=${Date.now()}`;
        const resp = await fetch(url, { cache: 'no-store' });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        const exists = data && data.exists === true;
        if (exists) {
          $dl.title = 'Download AI Report';
          $gen.textContent = 'Regenerate AI Report';
          if (showStatus) setStatus('Report is ready ✔');
        } else {
          $dl.title = 'Report not ready yet';
          $gen.textContent = 'Generate AI Report';
          if (showStatus) setStatus('Report not ready yet', true);
        }
        return exists;
      } catch (err) {
        console.error('Error checking report availability:', err);
        if (showStatus) setStatus('Error checking report', true);
        $dl.title = 'Report not ready yet';
        return false;
      }
    }

    async function generateReport() {
      // If a report already exists, confirm if user really wants to regenerate
      const exists = await checkReportExists(false);
      if (exists) {
        if (!confirm('A report is already available for this game. Do you want to generate a new updated report?')) {
          return;
        }
      }
      // Prepare data from the buttons’ data attributes
      const home_full  = $gen.dataset.homefull;
      const away_full  = $gen.dataset.awayfull;
      const home_short = $gen.dataset.homeshort;
      const away_short = $gen.dataset.awayshort;

      setStatus('The AI report is being generated. This can take a few minutes...', false);
      $gen.disabled = true;

      // Send POST request to start report generation
      fetch(`${API_BASE}/generate-report`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ 
          api_key: API_KEY,
          home_full, away_full, home_short, away_short 
        })
      })
      .then(async resp => {
        if (resp.ok) {
          setStatus('Report generation started. Please wait ~5 minutes then click Download.', false);
        } else {
          // If server returned an error, display it
          let errMsg = `Error starting report (HTTP ${resp.status})`;
          try {
            const errData = await resp.json();
            if (errData.error) errMsg = errData.error;
          } catch {}
          setStatus(errMsg, true);
        }
      })
      .catch(err => {
        console.error('Network error starting report generation:', err);
        setStatus('Network error – could not start report.', true);
      })
      .finally(() => {
        // Re-enable the Generate button after a brief delay
        setTimeout(() => { $gen.disabled = false; }, 1000);
      });

      // Optionally, poll the report status after some intervals to give user feedback
      setTimeout(() => checkReportExists(true), 15000);  // 15 seconds check
      setTimeout(() => checkReportExists(true), 60000);  // 60 seconds check
    }

    function downloadReport() {
      const home_short = $gen.dataset.homeshort;
      const away_short = $gen.dataset.awayshort;
      const ts = Date.now();  // cache-buster
      const url = `${API_BASE}/get-report?api_key=${encodeURIComponent(API_KEY)}&home_team=${encodeURIComponent(home_short)}&away_team=${encodeURIComponent(away_short)}&_=${ts}`;
      window.location.href = url;
    }

    // Initial check on page load for existing report
    checkReportExists(false);
    // Set up event listeners
    if ($chk) $chk.addEventListener('click', () => checkReportExists(true));
    $gen.addEventListener('click', generateReport);
    $dl.addEventListener('click', downloadReport);
  });
});
</script>
</body>
</html>
