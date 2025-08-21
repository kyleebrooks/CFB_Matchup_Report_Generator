<?php
include 'common.inc';
// Allow this script to run as long as needed without timing out
set_time_limit(0);
if (!SessionStarted())
    $ses = 'true';
else
    $ses = 'false';
require_once 'api_keys.php';

// Connect to the database
$connection = mysql_connect("p3nlmysql149plsk.secureserver.net", "kdogg4207", "xMkM2941")
    or die("Could not connect to database server");
mysql_select_db("kdogg4207", $connection)
    or die("Problem with database on server");

$weekID = null;
$year = null;

$weekResult = mysql_query("select weekID from week where currentWeek='true'", $connection) or die('Query failed.');
if ($row = mysql_fetch_array($weekResult, MYSQL_ASSOC)) {
    $weekID = $row['weekID'];
}

$yearResult = mysql_query("select year from year where currentYear='true'", $connection) or die('Query failed.');
if ($row = mysql_fetch_array($yearResult, MYSQL_ASSOC)) {
    $year = $row['year'];
}

// Load the logged in user's picks for this week
// Match picks to the team_logo table by team name so we can use the logo's
// team ID (which aligns with the CFBD API) when matching against the
// scoreboard data.
$userPicks = array();
if (isset($_SESSION['username'])) {
    $username = $_SESSION['username'];
    $memberResult = mysql_query("select memberid from member where username='$username' limit 1", $connection) or die('Query failed.');
    if ($row = mysql_fetch_array($memberResult, MYSQL_ASSOC)) {
        $memberId = $row['memberid'];
        mysql_free_result($memberResult);
        // Retrieve the CFBD team ID (via team_logo.id) and the normalized team name
        // for each pick. Some teams may not have an entry in the team_logo table,
        // so store both identifiers to improve matching later.
        $pickQuery = "select tl.id as logoId, lower(trim(t.teamname)) as teamName from pick p "
                   . "join team t on p.teamID = t.teamID "
                   . "left join team_logo tl on lower(trim(t.teamname)) = lower(trim(tl.team)) "
                   . "where p.memberID='$memberId' and p.weekID='$weekID' and p.yearID='$year'";
        $pickResult = mysql_query($pickQuery, $connection) or die('Query failed.');
        while ($row = mysql_fetch_array($pickResult, MYSQL_ASSOC)) {
            $logoId = isset($row['logoId']) ? (string)trim($row['logoId']) : '';
            $teamName = isset($row['teamName']) ? trim($row['teamName']) : '';
            if ($logoId !== '') {
                // Prefix with 'id:' so numeric strings are not converted to ints when used as keys
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

// Pull AFPLNA games for the current week using team IDs from the team_logo table
$afplnaGames = array();
$gamesQuery = "select tlh.id as homeId, tla.id as awayId "
            . "from game g "
            . "join team th on g.homeID = th.teamID "
            . "join team ta on g.awayID = ta.teamID "
            . "left join team_logo tlh on lower(trim(th.teamname)) = lower(trim(tlh.team)) "
            . "left join team_logo tla on lower(trim(ta.teamname)) = lower(trim(tla.team)) "
            . "where g.weekID = '$weekID' and g.yearID = '$year'";
$gamesResult = mysql_query($gamesQuery, $connection) or die('Query failed.');
while ($row = mysql_fetch_array($gamesResult, MYSQL_ASSOC)) {
    $homeId = isset($row['homeId']) ? (string)$row['homeId'] : '';
    $awayId = isset($row['awayId']) ? (string)$row['awayId'] : '';
    if ($homeId && $awayId) {
        $afplnaGames[$homeId . '|' . $awayId] = true;
    }
}
mysql_free_result($gamesResult);

// Load team logos and names from the database so that we can map CFBD team
// IDs to the normalized team names used within AFPLNA. These names will be
// used when calling the CFBD API to ensure consistency with the coach page.
$teamData = array();
$teamResult = mysql_query(
    "select tl.id, tl.url, t.teamname from team_logo tl " .
    "join team t on lower(trim(tl.team)) = lower(trim(t.teamname))",
    $connection
) or die('Query failed.');
while ($row = mysql_fetch_array($teamResult, MYSQL_ASSOC)) {
    $id = (string)trim($row['id']);
    $teamData[$id] = array(
        'logo' => trim($row['url']),
        'name' => trim($row['teamname'])
    );
}
mysql_free_result($teamResult);

// Retrieve API key for CollegeFootballData
$apiKey = '';
$keyResult = mysql_query("select `KEY` from API_KEYS where API_NAME='CFD' limit 1", $connection);
if ($keyResult && $row = mysql_fetch_array($keyResult, MYSQL_ASSOC)) {
    $apiKey = trim($row['KEY']);
    mysql_free_result($keyResult);
}
if (!$apiKey) {
    if (defined('CFBD_API_KEY') && CFBD_API_KEY) {
        $apiKey = CFBD_API_KEY;
    } elseif (!empty($CFBD_API_KEY)) {
        $apiKey = $CFBD_API_KEY;
    } else {
        $apiKey = getenv('CFBD_API_KEY');
    }
}

// Retrieve Google Gemini API key
$googleApiKey = '';
 $googleResult = mysql_query("select `KEY` from API_KEYS where API_NAME='google' limit 1", $connection);
 if ($googleResult && $row = mysql_fetch_array($googleResult, MYSQL_ASSOC)) {
     $googleApiKey = trim($row['KEY']);
     mysql_free_result($googleResult);
 }

// Where the AFPLNA Flask API lives and its API key
$AFPLNA_API_BASE = 'http://143.198.20.72';
$AFPLNA_API_KEY = '';
$afplnaKeyResult = mysql_query("select `KEY` from API_KEYS where API_NAME='cfbmatchupreport' limit 1", $connection);
if ($afplnaKeyResult && $row = mysql_fetch_array($afplnaKeyResult, MYSQL_ASSOC)) {
    $AFPLNA_API_KEY = trim($row['KEY']);
    mysql_free_result($afplnaKeyResult);
}

// Call the live scoreboard endpoint
$url = "https://api.collegefootballdata.com/scoreboard?classification=fbs";
$ch = curl_init($url);
curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
$headers = array("accept: application/json");
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

$featuredGames = array();
$otherGames = array();
foreach ($data as $game) {
    $homeName = isset($game['homeTeam']['name']) ? $game['homeTeam']['name'] : '';
    $awayName = isset($game['awayTeam']['name']) ? $game['awayTeam']['name'] : '';
    // Cast API team IDs to string for consistent lookup in the logo table and AFPLNA matching
    $homeId = isset($game['homeTeam']['id']) ? (string)$game['homeTeam']['id'] : '';
    $awayId = isset($game['awayTeam']['id']) ? (string)$game['awayTeam']['id'] : '';
    $key = $homeId . '|' . $awayId;
    $yourPick = '';
    // Determine if the user picked one of these teams. First check by CFBD team
    // ID, then fall back to a normalized team name match if needed.
    $homeNameNorm = strtolower(trim($homeName));
    $awayNameNorm = strtolower(trim($awayName));
    if (isset($userPicks['id:' . $homeId]) || isset($userPicks['name:' . $homeNameNorm])) {
        $yourPick = $homeName;
    } elseif (isset($userPicks['id:' . $awayId]) || isset($userPicks['name:' . $awayNameNorm])) {
        $yourPick = $awayName;
    }
    $info = array(
        // Use the API-provided names for display but store database team
        // names separately for use with the CFBD API and AI reporting.
        'home' => $homeName,
        'away' => $awayName,
        'homeDbName' => isset($teamData[$homeId]['name']) ? $teamData[$homeId]['name'] : $homeName,
        'awayDbName' => isset($teamData[$awayId]['name']) ? $teamData[$awayId]['name'] : $awayName,

        // Match team IDs against the logo table to populate logo URLs
        'homeLogo' => isset($teamData[$homeId]['logo']) ? $teamData[$homeId]['logo'] : '',
        'awayLogo' => isset($teamData[$awayId]['logo']) ? $teamData[$awayId]['logo'] : '',

        'venue' => isset($game['venue']['name']) ? $game['venue']['name'] : '',
        'start' => isset($game['startDate']) ? date('n/j g:i A', strtotime($game['startDate'])) : '',
        'tv' => isset($game['tv']) ? $game['tv'] : '',
        'status' => isset($game['status']) ? $game['status'] : '',
        'period' => isset($game['period']) ? $game['period'] : '',
        'clock' => isset($game['clock']) ? $game['clock'] : '',
        'situation' => isset($game['situation']) ? $game['situation'] : '',
        'possession' => isset($game['possession']) ? $game['possession'] : '',
        'lastPlay' => isset($game['lastPlay']) ? $game['lastPlay'] : '',
        'homePoints' => isset($game['homeTeam']['points']) ? $game['homeTeam']['points'] : '',
        'awayPoints' => isset($game['awayTeam']['points']) ? $game['awayTeam']['points'] : '',
        'windDir' => isset($game['weather']['windDirection']) ? $game['weather']['windDirection'] : '',
        'windSpeed' => isset($game['weather']['windSpeed']) ? $game['weather']['windSpeed'] : '',
        'weatherDesc' => isset($game['weather']['description']) ? $game['weather']['description'] : '',
        'temperature' => isset($game['weather']['temperature']) ? $game['weather']['temperature'] : '',
        'awayML' => isset($game['betting']['awayMoneyline']) ? $game['betting']['awayMoneyline'] : '',
        'homeML' => isset($game['betting']['homeMoneyline']) ? $game['betting']['homeMoneyline'] : '',
        'overUnder' => isset($game['betting']['overUnder']) ? $game['betting']['overUnder'] : '',
        'spread' => isset($game['betting']['spread']) ? $game['betting']['spread'] : '',
        'yourPick' => $yourPick
    );
    if (isset($afplnaGames[$key])) {
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
.game { border-radius: 8px; overflow: hidden; margin: 20px 0; box-shadow: 0 2px 6px rgba(0,0,0,0.15); background: #fff; }
.game.afplna { border: 2px solid gold; }
.score-header { display: flex; justify-content: space-between; align-items: center; background: #003366; color: #fff; padding: 10px; font-size: 18px; font-weight: bold; }
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

        $awayLogo = $g['awayLogo'];
        $homeLogo = $g['homeLogo'];

        echo "<div class='team-name'>";
        if ($awayLogo) {
            echo "<img src='" . htmlspecialchars($awayLogo) . "' class='team-logo'>";
        }
        echo htmlspecialchars($g['away']) . "</div>";
        echo "<div class='score'>" . htmlspecialchars($g['awayPoints']) . " @ " . htmlspecialchars($g['homePoints']) . "</div>";
        echo "<div class='team-name'>";
        if ($homeLogo) {
            echo "<img src='" . htmlspecialchars($homeLogo) . "' class='team-logo'>";
        }
        echo htmlspecialchars($g['home']) . "</div>";
        echo "</div>";
        echo "<div class='game-details'>";
        echo "<div><b>Venue:</b> " . htmlspecialchars($g['venue']) . " | <b>Start:</b> " . htmlspecialchars($g['start']) . " | <b>TV:</b> " . htmlspecialchars($g['tv']) . "</div>";
        echo "<div><b>Status:</b> " . htmlspecialchars($g['status']) . " | <b>Period:</b> " . htmlspecialchars($g['period']) . " | <b>Clock:</b> " . htmlspecialchars($g['clock']) . "</div>";
        echo "<div><b>Situation:</b> " . htmlspecialchars($g['situation']) . " | <b>Possession:</b> " . htmlspecialchars($g['possession']) . "</div>";
        echo "<div><b>Last Play:</b> " . htmlspecialchars($g['lastPlay']) . "</div>";
        echo "<div><b>Weather:</b> " . htmlspecialchars($g['weatherDesc']) . ", Temp " . htmlspecialchars($g['temperature']) . "&deg;, Wind " . htmlspecialchars($g['windDir']) . "&deg; @ " . htmlspecialchars($g['windSpeed']) . " mph</div>";
        echo "<div><b>Lines:</b> Away ML " . htmlspecialchars($g['awayML']) . ", Home ML " . htmlspecialchars($g['homeML']) . ", O/U " . htmlspecialchars($g['overUnder']) . ", Spread " . htmlspecialchars($g['spread']) . "</div>";
        if (!empty($g['yourPick'])) {
            echo "<div><b>Your Pick:</b> " . htmlspecialchars($g['yourPick']) . "</div>";
        }
        echo '<div class="ai-controls" style="margin:12px 0;">';
        echo '<button type="button" class="btn-generate" data-homefull="' . htmlspecialchars($g['home'], ENT_QUOTES) . '" data-awayfull="' . htmlspecialchars($g['away'], ENT_QUOTES) . '" data-homeshort="' . htmlspecialchars($g['homeDbName'], ENT_QUOTES) . '" data-awayshort="' . htmlspecialchars($g['awayDbName'], ENT_QUOTES) . '">Generate AI Report</button>';
        echo '<button type="button" class="btn-download">Download AI Report</button>';
        echo '<span class="ai-status" style="margin-left:10px;color:#0a0;">&nbsp;</span>';
        echo '</div>';
        echo "</div>";
        echo "</div>";
    }
}
if (!empty($otherGames)) {
    echo "<h2 class='section-title'>All FBS Games</h2>";
    foreach ($otherGames as $g) {
        echo "<div class='game'>";
        echo "<div class='score-header'>";

        $awayLogo = $g['awayLogo'];
        $homeLogo = $g['homeLogo'];

        echo "<div class='team-name'>";
        if ($awayLogo) {
            echo "<img src='" . htmlspecialchars($awayLogo) . "' class='team-logo'>";
        }
        echo htmlspecialchars($g['away']) . "</div>";
        echo "<div class='score'>" . htmlspecialchars($g['awayPoints']) . " @ " . htmlspecialchars($g['homePoints']) . "</div>";
        echo "<div class='team-name'>";
        if ($homeLogo) {
            echo "<img src='" . htmlspecialchars($homeLogo) . "' class='team-logo'>";
        }
        echo htmlspecialchars($g['home']) . "</div>";
        echo "</div>";
        echo "<div class='game-details'>";
        echo "<div><b>Venue:</b> " . htmlspecialchars($g['venue']) . " | <b>Start:</b> " . htmlspecialchars($g['start']) . " | <b>TV:</b> " . htmlspecialchars($g['tv']) . "</div>";
        echo "<div><b>Status:</b> " . htmlspecialchars($g['status']) . " | <b>Period:</b> " . htmlspecialchars($g['period']) . " | <b>Clock:</b> " . htmlspecialchars($g['clock']) . "</div>";
        echo "<div><b>Situation:</b> " . htmlspecialchars($g['situation']) . " | <b>Possession:</b> " . htmlspecialchars($g['possession']) . "</div>";
        echo "<div><b>Last Play:</b> " . htmlspecialchars($g['lastPlay']) . "</div>";
        echo "<div><b>Weather:</b> " . htmlspecialchars($g['weatherDesc']) . ", Temp " . htmlspecialchars($g['temperature']) . "&deg;, Wind " . htmlspecialchars($g['windDir']) . "&deg; @ " . htmlspecialchars($g['windSpeed']) . " mph</div>";
        echo "<div><b>Lines:</b> Away ML " . htmlspecialchars($g['awayML']) . ", Home ML " . htmlspecialchars($g['homeML']) . ", O/U " . htmlspecialchars($g['overUnder']) . ", Spread " . htmlspecialchars($g['spread']) . "</div>";
        echo '<div class="ai-controls" style="margin:12px 0;">';
        echo '<button type="button" class="btn-generate" data-homefull="' . htmlspecialchars($g['home'], ENT_QUOTES) . '" data-awayfull="' . htmlspecialchars($g['away'], ENT_QUOTES) . '" data-homeshort="' . htmlspecialchars($g['homeDbName'], ENT_QUOTES) . '" data-awayshort="' . htmlspecialchars($g['awayDbName'], ENT_QUOTES) . '">Generate AI Report</button>';
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
const API_BASE = "<?= $AFPLNA_API_BASE ?>";
const API_KEY  = "<?= $AFPLNA_API_KEY ?>";

window.addEventListener('DOMContentLoaded', () => {
document.querySelectorAll('.ai-controls').forEach(ctrl => {
  const $gen = ctrl.querySelector('.btn-generate');
  const $dl  = ctrl.querySelector('.btn-download');
  const $st  = ctrl.querySelector('.ai-status');

  function setStatus(msg, isErr=false) {
    $st.textContent = msg;
    $st.style.color = isErr ? '#c00' : '#0a0';
    $st.style.backgroundColor = (!isErr && msg) ? '#cfc' : 'transparent';
    $st.style.padding = (!isErr && msg) ? '2px 4px' : '0';
  }

  async function checkReportExists(showStatus = false) {
    const home_short = $gen.dataset.homeshort;
    const away_short = $gen.dataset.awayshort;
    // add a cache-busting query param so the browser doesn't reuse stale
    // results and incorrectly report that no file exists
    const ts = Date.now();

    try {
      const resp = await fetch(
        `${API_BASE}/has-report?api_key=${encodeURIComponent(API_KEY)}&home_team=${encodeURIComponent(home_short)}&away_team=${encodeURIComponent(away_short)}&_=${ts}`,
        { cache: 'no-store' }
      );
      if (resp.ok) {
        const data = await resp.json();
        if (data && data.exists) {
          if (showStatus) setStatus('Available!');
          return true;
        }
      }
    } catch (err) {
      console.log('Error checking report availability', err);
    }

    if (showStatus) setStatus('');
    return false;
  }

  async function generateReport() {
    const exists = await checkReportExists(false);
    let force = false;
    if (exists) {
      const proceed = confirm('A report is already available for this game. Do you want to regenerate a newer one?');
      if (!proceed) return;
      force = true;
    }

    const home_full  = $gen.dataset.homefull;
    const away_full  = $gen.dataset.awayfull;
    const home_short = $gen.dataset.homeshort;
    const away_short = $gen.dataset.awayshort;

    setStatus('The AI report is being generated. This can take up to 5 minutes. Try the download report button after 5 minutes to receive the report.');
    $gen.disabled = true;

    fetch(`${API_BASE}/generate-report`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        api_key: API_KEY,
        home_full, away_full, home_short, away_short, force
      })
    })
    .then(resp => {
      if (resp && resp.ok) {
        setStatus('Report generation started. This can take up to 5 minutes. Try the download report button after 5 minutes to receive the report.');
      }
    })
    .catch(err => {
      console.log('Network error starting report generation.', err);
    });

    setTimeout(() => { $gen.disabled = false; }, 1000);
  }

  function downloadReport() {
    const home_short = $gen.dataset.homeshort;
    const away_short = $gen.dataset.awayshort;

    // cache-buster to avoid any weird proxy caching
    const ts = Date.now();
    const url = `${API_BASE}/get-report?api_key=${encodeURIComponent(API_KEY)}&home_team=${encodeURIComponent(home_short)}&away_team=${encodeURIComponent(away_short)}&_=${ts}`;

    // Simple, reliable download via navigation
    window.location.href = url;
  }

  // Initial availability check on load
  checkReportExists(true);

  $gen.addEventListener('click', generateReport);
  $dl.addEventListener('click', downloadReport);
});
});
</script>
</body>
</html>