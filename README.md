# Fantasy NBA Weekly Games Grid & Back-to-Back Finder

An agent skill for fantasy basketball managers. It shows each NBA team's games this week (Monday-Sunday) and next week, back-to-backs, and light days with few games — the days you are likely to have open lineup slots for a streamer — with tip-off times in your time zone. Paste your roster and it adds games per player and the open starting slots for each day.

It is a standard agent skill: a `SKILL.md` that tells the agent what to run, and `run.py` (Python 3.8+, standard library only, nothing to install).

## Install

Claude Code (or any agent that reads `SKILL.md` skills):

```bash
git clone https://github.com/dh-soft/fantasy-nba-games-grid
cp -r fantasy-nba-games-grid ~/.claude/skills/free-nba-games-grid
```

or with the skills CLI:

```bash
npx skills add dh-soft/fantasy-nba-games-grid
```

Then ask your agent, for example: "Show this week's NBA grid for my roster."

## Your free API key

The schedule comes from [BALLDONTLIE](https://www.balldontlie.io) with your own free key: sign up at https://app.balldontlie.io, copy the key, then set `BALLDONTLIE_API_KEY` or run `python3 run.py setkey` and paste it (input is hidden). The key is stored only on your computer and sent only to api.balldontlie.io.

## Commands

| What | Command |
|---|---|
| This week's grid for all teams | `python3 run.py grid` |
| Your roster's games and open slots | `python3 run.py grid --roster "Victor Wembanyama, Luka Doncic, ..." --slots 10` |
| Show tip-off times in your time zone | `python3 run.py settings --tz America/New_York` |
| Save your API key (input hidden) | `python3 run.py setkey` |
| Check key and cache | `python3 run.py status` |

## Example output

See [examples/sample-output.md](examples/sample-output.md) (a real run with a sample roster).

## Limits

- Schedules come from the BALLDONTLIE API; postponed or rescheduled games appear after the next refresh.
- BALLDONTLIE's free plan allows 5 requests per minute; the first run can take about a minute.
- Games per player use the player's current team; recent trades can take a refresh to show.
- Unofficial; not affiliated with the NBA or any team. Not betting or daily fantasy (DFS) advice.

## Want it checked automatically?

[Fantasy NBA Roster Watch](https://capafy.ai/agent/fantasy-nba-roster-watch-start-sit-streamers/4740205927) on Capafy checks your roster every 4 hours with injuries, minutes trends, start/sit and streamers for your weak categories.

## License

MIT — see [LICENSE](LICENSE). Made by DH Soft.
