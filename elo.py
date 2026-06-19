import sqlite3
import os
import csv
import sys
from datetime import datetime

# Fallback for dependencies if not installed
try:
    from colorama import Fore, Style, init
    init(autoreset=True)
except ImportError:
    class FakeColor:
        def __getattr__(self, name):
            return ""
    Fore = FakeColor()
    Style = FakeColor()

try:
    from tabulate import tabulate
except ImportError:
    def tabulate(rows, headers, tablefmt=None):
        header_line = " | ".join(headers)
        border = "-" * len(header_line)
        lines = [header_line, border]
        for r in rows:
            lines.append(" | ".join(map(str, r)))
        return "\n".join(lines)

DB_PATH = "./data/elo/db/data.db"
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
con = sqlite3.connect(DB_PATH)
cur = con.cursor()

def initialize_tables():
    """SQLite 테이블 초기화"""
    # 리더보드 테이블: 1v1, 2v2, Combined ELO를 각각 기록
    cur.execute('''
    CREATE TABLE IF NOT EXISTS leaderboard (
        nickname TEXT PRIMARY KEY,
        kills_1v1 INTEGER DEFAULT 0,
        deaths_1v1 INTEGER DEFAULT 0,
        wins_1v1 INTEGER DEFAULT 0,
        losses_1v1 INTEGER DEFAULT 0,
        total_1v1 INTEGER DEFAULT 0,
        elo_1v1 INTEGER DEFAULT 1500,
        tier_1v1 TEXT DEFAULT 'Unranked',
        
        kills_2v2 INTEGER DEFAULT 0,
        deaths_2v2 INTEGER DEFAULT 0,
        wins_2v2 INTEGER DEFAULT 0,
        losses_2v2 INTEGER DEFAULT 0,
        total_2v2 INTEGER DEFAULT 0,
        elo_2v2 INTEGER DEFAULT 1500,
        tier_2v2 TEXT DEFAULT 'Unranked',
        
        elo_combined INTEGER DEFAULT 1500
    )
    ''')

    # 매치 기록 테이블
    cur.execute('''
    CREATE TABLE IF NOT EXISTS matches (
        match_id INTEGER PRIMARY KEY AUTOINCREMENT,
        round INTEGER,
        mode TEXT, -- '1v1' or '2v2'
        map TEXT,
        nickname TEXT,
        team TEXT, -- 'RED' or 'BLUE'
        kills INTEGER,
        deaths INTEGER,
        winning_team TEXT, -- 'RED' or 'BLUE'
        score TEXT, -- '5:3', '4:2' 등
        registrant TEXT, -- 등록 관리자 이름
        FOREIGN KEY(nickname) REFERENCES leaderboard(nickname) ON DELETE CASCADE
    )
    ''')

    # ELO 변동 이력 테이블
    cur.execute('''
    CREATE TABLE IF NOT EXISTS aftercalc (
        ac_id INTEGER PRIMARY KEY AUTOINCREMENT,
        round INTEGER,
        nickname TEXT,
        mode TEXT, -- '1v1' or '2v2'
        elo_after_round INTEGER,
        kdr_after_round REAL,
        FOREIGN KEY(nickname) REFERENCES leaderboard(nickname) ON DELETE CASCADE,
        UNIQUE(round, nickname, mode)
    )
    ''')
    con.commit()

def calculate_tier(elo):
    """ELO 점수에 따라 티어 산정"""
    base_elo = 1500
    tier_names = [
        "Copper 3", "Copper 2", "Copper 1",
        "Bronze 3", "Bronze 2", "Bronze 1",
        "Silver 3", "Silver 2", "Silver 1",
        "Gold 3", "Gold 2", "Gold 1",
        "Platinum 3", "Platinum 2", "Platinum 1",
        "Diamond 3", "Diamond 2", "Diamond 1",
        "REDLINER"
    ]
    tier_index = (elo - base_elo) // 100 + 9
    tier_index = max(0, min(tier_index, len(tier_names) - 1))
    return tier_names[tier_index]

def round_half_up(n):
    """지정값을 소수점 첫째자리에서 반올림 (JS Math.round와 동일하게 작동)"""
    return int(n + 0.5) if n >= 0 else int(n - 0.5)

def calculate_elo(player_elo, opponent_avg_elo, result, kills, deaths, player_total_matches, player_wins, 
                  k_normal=30, kill_weight=0.2, death_weight=0.07, initial_k_factor=50, initial_matches_threshold=5):
    """KPFM 공식 기반 ELO 점수 계산"""
    if player_total_matches <= initial_matches_threshold:
        base_k = initial_k_factor
    else:
        base_k = k_normal / (1 + (player_total_matches - initial_matches_threshold) / 20)
        base_k = max(base_k, k_normal / 2)

    expected_result = 1 / (1 + 10 ** ((opponent_avg_elo - player_elo) / 200))
    win_loss_numeric_factor = 1 if result == 1 else -1
    elo_change_match_outcome = base_k * win_loss_numeric_factor * (1.5 - expected_result)
    
    performance_score = (kills * kill_weight) - (deaths * death_weight)
    if result == 0:
        performance_score *= 0.5  # 패배 시 KDA 보너스 패널티 감쇄

    new_elo = player_elo + elo_change_match_outcome + performance_score
    return max(0, round_half_up(new_elo))

def recalculate_all():
    """모든 경기 기록을 처음부터 다시 굴려 ELO 및 리더보드 통계 재연산"""
    # 1. 리더보드 초기화
    cur.execute("SELECT nickname FROM leaderboard")
    players = [r[0] for r in cur.fetchall()]
    
    # 임시 플레이어 메모리 초기화
    player_stats = {}
    for p in players:
        player_stats[p] = {
            'nickname': p,
            'kills_1v1': 0, 'deaths_1v1': 0, 'wins_1v1': 0, 'losses_1v1': 0, 'total_1v1': 0, 'elo_1v1': 1500,
            'kills_2v2': 0, 'deaths_2v2': 0, 'wins_2v2': 0, 'losses_2v2': 0, 'total_2v2': 0, 'elo_2v2': 1500,
        }

    # aftercalc 기록 제거
    cur.execute("DELETE FROM aftercalc")
    
    # 2. 모든 매치를 라운드 순으로 조회
    cur.execute("SELECT round, mode, map, nickname, team, kills, deaths, winning_team, score, registrant FROM matches ORDER BY round ASC")
    all_rows = cur.fetchall()
    
    if not all_rows:
        con.commit()
        export_to_csv()
        return

    # 라운드별 그룹화
    rounds_data = {}
    for row in all_rows:
        rnd = row[0]
        if rnd not in rounds_data:
            rounds_data[rnd] = []
        rounds_data[rnd].append(row)

    sorted_rounds = sorted(rounds_data.keys())
    
    for rnd in sorted_rounds:
        matches_in_round = rounds_data[rnd]
        mode = matches_in_round[0][1]
        winning_team = matches_in_round[0][7]
        
        red_team_players = []
        blue_team_players = []
        
        for match in matches_in_round:
            nick = match[3]
            team = match[4]
            k = match[5]
            d = match[6]
            
            # 플레이어가 로컬 캐시에 없으면 추가
            if nick not in player_stats:
                player_stats[nick] = {
                    'nickname': nick,
                    'kills_1v1': 0, 'deaths_1v1': 0, 'wins_1v1': 0, 'losses_1v1': 0, 'total_1v1': 0, 'elo_1v1': 1500,
                    'kills_2v2': 0, 'deaths_2v2': 0, 'wins_2v2': 0, 'losses_2v2': 0, 'total_2v2': 0, 'elo_2v2': 1500,
                }
            
            p_data = player_stats[nick]
            # ELO 계산용 사전 값 백업
            elo_key = 'elo_1v1' if mode == '1v1' else 'elo_2v2'
            pre_elo = p_data[elo_key]
            
            player_info = {
                'nickname': nick,
                'kills': k,
                'deaths': d,
                'team': team,
                'pre_elo': pre_elo
            }
            
            if team == 'RED':
                red_team_players.append(player_info)
            else:
                blue_team_players.append(player_info)

        # 각 팀 평균 ELO 산정
        red_avg = sum(p['pre_elo'] for p in red_team_players) / len(red_team_players) if red_team_players else 1500
        blue_avg = sum(p['pre_elo'] for p in blue_team_players) / len(blue_team_players) if blue_team_players else 1500
        
        # 각 플레이어 ELO 갱신 및 aftercalc 기록 작성
        for team_players, opp_avg_elo, team_name in [(red_team_players, blue_avg, 'RED'), (blue_team_players, red_avg, 'BLUE')]:
            result = 1 if team_name == winning_team else 0
            for p in team_players:
                nick = p['nickname']
                p_cache = player_stats[nick]
                
                if mode == '1v1':
                    new_elo = calculate_elo(
                        p_cache['elo_1v1'], opp_avg_elo, result, p['kills'], p['deaths'], 
                        p_cache['total_1v1'], p_cache['wins_1v1']
                    )
                    # 통계 갱신
                    p_cache['elo_1v1'] = new_elo
                    p_cache['kills_1v1'] += p['kills']
                    p_cache['deaths_1v1'] += p['deaths']
                    p_cache['wins_1v1'] += result
                    p_cache['losses_1v1'] += (1 - result)
                    p_cache['total_1v1'] += 1
                    
                    cum_kdr = p_cache['kills_1v1'] / max(1, p_cache['deaths_1v1'])
                    
                    # aftercalc 기록 추가
                    cur.execute(
                        "INSERT INTO aftercalc (round, nickname, mode, elo_after_round, kdr_after_round) VALUES (?, ?, ?, ?, ?)",
                        (rnd, nick, '1v1', new_elo, round(cum_kdr, 2))
                    )
                else:  # 2v2
                    new_elo = calculate_elo(
                        p_cache['elo_2v2'], opp_avg_elo, result, p['kills'], p['deaths'], 
                        p_cache['total_2v2'], p_cache['wins_2v2']
                    )
                    # 통계 갱신
                    p_cache['elo_2v2'] = new_elo
                    p_cache['kills_2v2'] += p['kills']
                    p_cache['deaths_2v2'] += p['deaths']
                    p_cache['wins_2v2'] += result
                    p_cache['losses_2v2'] += (1 - result)
                    p_cache['total_2v2'] += 1
                    
                    cum_kdr = p_cache['kills_2v2'] / max(1, p_cache['deaths_2v2'])
                    
                    # aftercalc 기록 추가
                    cur.execute(
                        "INSERT INTO aftercalc (round, nickname, mode, elo_after_round, kdr_after_round) VALUES (?, ?, ?, ?, ?)",
                        (rnd, nick, '2v2', new_elo, round(cum_kdr, 2))
                    )

    # 3. 리더보드 테이블 테이블 업데이트
    cur.execute("DELETE FROM leaderboard")
    for nick, s in player_stats.items():
        t_1v1 = calculate_tier(s['elo_1v1'])
        t_2v2 = calculate_tier(s['elo_2v2'])
        comb_elo = round_half_up((s['elo_1v1'] + s['elo_2v2']) / 2)
        
        cur.execute('''
            INSERT INTO leaderboard (
                nickname, kills_1v1, deaths_1v1, wins_1v1, losses_1v1, total_1v1, elo_1v1, tier_1v1,
                kills_2v2, deaths_2v2, wins_2v2, losses_2v2, total_2v2, elo_2v2, tier_2v2, elo_combined
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            nick, s['kills_1v1'], s['deaths_1v1'], s['wins_1v1'], s['losses_1v1'], s['total_1v1'], s['elo_1v1'], t_1v1,
            s['kills_2v2'], s['deaths_2v2'], s['wins_2v2'], s['losses_2v2'], s['total_2v2'], s['elo_2v2'], t_2v2, comb_elo
        ))
    
    con.commit()
    export_to_csv()
    print(Fore.GREEN + "[OK] ELO 및 리더보드 재계산 완료 및 CSV 파일 동기화 성공!")

def export_to_csv():
    """DB 데이터를 CSV 파일로 내보내기"""
    # 1. lboard.csv
    cur.execute('''
        SELECT nickname, elo_1v1, kills_1v1, deaths_1v1, wins_1v1, losses_1v1, total_1v1, tier_1v1,
               elo_2v2, kills_2v2, deaths_2v2, wins_2v2, losses_2v2, total_2v2, tier_2v2, elo_combined
        FROM leaderboard ORDER BY elo_combined DESC
    ''')
    lboard_rows = cur.fetchall()
    with open("lboard.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Nickname", "Elo1v1", "Kills1v1", "Deaths1v1", "Wins1v1", "Losses1v1", "Matches1v1", "Tier1v1",
            "Elo2v2", "Kills2v2", "Deaths2v2", "Wins2v2", "Losses2v2", "Matches2v2", "Tier2v2", "EloCombined"
        ])
        writer.writerows(lboard_rows)

    # 2. match.csv
    cur.execute('''
        SELECT round, mode, map, nickname, kills, deaths, team, winning_team, score, registrant
        FROM matches ORDER BY round ASC, team DESC, nickname ASC
    ''')
    match_rows = cur.fetchall()
    with open("match.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Round", "Mode", "Map", "Nickname", "Kills", "Deaths", "Team", "Winning Team", "Score", "Registrant"
        ])
        writer.writerows(match_rows)

    # 3. aftercalc.csv
    cur.execute('''
        SELECT round, nickname, mode, elo_after_round, kdr_after_round
        FROM aftercalc ORDER BY round ASC, nickname ASC
    ''')
    aftercalc_rows = cur.fetchall()
    with open("aftercalc.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["Round", "Nickname", "Mode", "EloAfterRound", "KdrAfterRound"])
        writer.writerows(aftercalc_rows)

def import_csv_to_db():
    """CSV 파일의 데이터를 읽어서 SQLite DB에 채워 넣기 (DB가 빈 경우 자동 마이그레이션)"""
    if not os.path.exists("match.csv"):
        return
        
    print(Fore.YELLOW + "[i] 로컬 CSV 파일을 감지했습니다. 데이터베이스로 마이그레이션합니다...")
    cur.execute("DELETE FROM matches")
    cur.execute("DELETE FROM leaderboard")
    cur.execute("DELETE FROM aftercalc")
    
    with open("match.csv", "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader)
        for row in reader:
            if not row: continue
            rnd, mode, map_name, nick, k, d, team, win_team, score, reg = row
            
            # 플레이어 선등록
            cur.execute("INSERT OR IGNORE INTO leaderboard (nickname) VALUES (?)", (nick,))
            
            # 매치 저장
            cur.execute('''
                INSERT INTO matches (round, mode, map, nickname, kills, deaths, team, winning_team, score, registrant)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (int(rnd), mode, map_name, nick, int(k), int(d), team, win_team, score, reg))
            
    con.commit()
    recalculate_all()

def add_scrim_prompt():
    """신규 스크림 경기 등록 프롬프트"""
    print(Fore.CYAN + "\n=== 신규 스크림 전적 입력 ===")
    
    # 1. 모드 선택
    while True:
        mode_input = input(">> 모드 선택 (1: 1v1 Duels, 2: 2v2 Wingman): ").strip()
        if mode_input == '1':
            mode = '1v1'
            max_wins = 5
            break
        elif mode_input == '2':
            mode = '2v2'
            max_wins = 4
            break
        print(Fore.RED + "⚠ 잘못된 입력입니다. 1 또는 2를 입력하세요.")

    # 2. 맵 선택
    maps_1v1 = ['highrise', 'kabuki', 'arc']
    maps_2v2 = ['metro']
    
    while True:
        if mode == '1v1':
            print(f"1대1 맵 리스트: {', '.join(maps_1v1)}")
            map_name = input(">> 맵 이름 입력: ").strip().lower()
            if map_name in maps_1v1:
                break
        else:
            print(f"2대2 맵 리스트: {', '.join(maps_2v2)}")
            map_name = input(">> 맵 이름 입력: ").strip().lower()
            if map_name in maps_2v2:
                break
        print(Fore.RED + f"⚠ {mode} 모드에 허용되지 않는 맵 이름입니다. 다시 입력해 주세요.")

    # 3. 라운드 번호 결정
    cur.execute("SELECT MAX(round) FROM matches")
    latest_round = cur.fetchone()[0]
    next_round = (latest_round + 1) if latest_round is not None else 1
    print(f"이번 매치는 라운드 #{next_round}로 기록됩니다.")

    # 4. 플레이어 입력
    if mode == '1v1':
        red_player = input(">> 레드(RED) 팀 플레이어 닉네임: ").strip()
        blue_player = input(">> 블루(BLUE) 팀 플레이어 닉네임: ").strip()
        red_team = [red_player]
        blue_team = [blue_player]
    else:
        red_players_input = input(">> 레드(RED) 팀 플레이어들 (띄어쓰기로 구분, 예: A B): ").strip().split()
        blue_players_input = input(">> 블루(BLUE) 팀 플레이어들 (띄어쓰기로 구분, 예: C D): ").strip().split()
        if len(red_players_input) != 2 or len(blue_players_input) != 2:
            print(Fore.RED + "⚠ 2대2 모드는 각 팀당 2명의 플레이어가 정확히 등록되어야 합니다.")
            return
        red_team = red_players_input
        blue_team = blue_players_input

    # 5. 스코어 입력
    while True:
        try:
            score_input = input(">> 최종 스코어 입력 (레드 스코어 블루 스코어, 예: 5 3): ").strip().split()
            red_score = int(score_input[0])
            blue_score = int(score_input[1])
            
            if red_score < 0 or blue_score < 0:
                raise ValueError
                
            # 스코어 승리 기준 검증
            if mode == '1v1':
                if red_score == 5 or blue_score == 5:
                    if red_score != blue_score:
                        break
                print(Fore.RED + "⚠ 1v1 듀얼은 먼저 5점에 선도달하는 5선승제 규칙을 지켜야 합니다 (예: 5대3, 2대5).")
            else:
                if red_score == 4 or blue_score == 4:
                    if red_score != blue_score:
                        break
                print(Fore.RED + "⚠ 2v2 윙맨은 먼저 4점에 선도달하는 4선승제 규칙을 지켜야 합니다 (예: 4대2, 1대4).")
        except (ValueError, IndexError):
            print(Fore.RED + "⚠ 잘못된 스코어 입력입니다. '정수 정수' 형태로 다시 입력하세요.")

    winning_team = 'RED' if red_score > blue_score else 'BLUE'
    score_str = f"{red_score}:{blue_score}"

    # 6. 개별 킬/데스 입력 (목숨 = 점수이므로 점수 기반 자동 매핑 또는 개별 수동 지정 지원)
    player_kd = {}
    use_manual_kd = input(">> 개별 킬/데스를 수동으로 직접 입력하시겠습니까? (y/n, n 입력 시 스코어 기준 자동 배분): ").strip().lower()
    
    if use_manual_kd == 'y':
        for p in red_team + blue_team:
            while True:
                try:
                    kd_inp = input(f"   [{p}] 킬 데스 입력 (예: 3 2): ").strip().split()
                    pk, pd = int(kd_inp[0]), int(kd_inp[1])
                    if pk < 0 or pd < 0: raise ValueError
                    player_kd[p] = (pk, pd)
                    break
                except (ValueError, IndexError):
                    print(Fore.RED + "   ⚠ 올바르지 않은 값입니다. '킬 데스' 양의 정수로 입력해 주세요.")
    else:
        # 자동 배분
        if mode == '1v1':
            player_kd[red_team[0]] = (red_score, blue_score)
            player_kd[blue_team[0]] = (blue_score, red_score)
        else:
            # 2대2의 경우 팀 스코어를 절반씩 나눠 기본값 채움 (레드 4점 승 시 인당 2킬씩, 패배팀 인당 1킬씩)
            red_k_base = red_score // 2
            blue_k_base = blue_score // 2
            
            player_kd[red_team[0]] = (red_k_base + (red_score % 2), blue_score // 2)
            player_kd[red_team[1]] = (red_k_base, blue_score // 2 + (blue_score % 2))
            
            player_kd[blue_team[0]] = (blue_k_base + (blue_score % 2), red_score // 2)
            player_kd[blue_team[1]] = (blue_k_base, red_score // 2 + (red_score % 2))

    registrant = input(">> 등록자(관리자 닉네임): ").strip()
    if not registrant:
        registrant = "admin"

    # DB 삽입
    for p in red_team:
        cur.execute("INSERT OR IGNORE INTO leaderboard (nickname) VALUES (?)", (p,))
        cur.execute('''
            INSERT INTO matches (round, mode, map, nickname, team, kills, deaths, winning_team, score, registrant)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (next_round, mode, map_name, p, 'RED', player_kd[p][0], player_kd[p][1], winning_team, score_str, registrant))
        
    for p in blue_team:
        cur.execute("INSERT OR IGNORE INTO leaderboard (nickname) VALUES (?)", (p,))
        cur.execute('''
            INSERT INTO matches (round, mode, map, nickname, team, kills, deaths, winning_team, score, registrant)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (next_round, mode, map_name, p, 'BLUE', player_kd[p][0], player_kd[p][1], winning_team, score_str, registrant))

    con.commit()
    recalculate_all()

def delete_round_prompt():
    """라운드 데이터 삭제"""
    print(Fore.CYAN + "\n=== 특정 라운드 매치 기록 삭제 ===")
    try:
        round_to_del = int(input(">> 삭제할 라운드 번호를 입력하세요: ").strip())
        cur.execute("SELECT COUNT(*) FROM matches WHERE round=?", (round_to_del,))
        count = cur.fetchone()[0]
        if count == 0:
            print(Fore.RED + "⚠ 해당 라운드 기록이 존재하지 않습니다.")
            return
            
        confirm = input(f"라운드 #{round_to_del}의 매치({count}개 행)를 정말 삭제하시겠습니까? (y/n): ").strip().lower()
        if confirm == 'y':
            cur.execute("DELETE FROM matches WHERE round=?", (round_to_del,))
            # 그 이상 라운드들의 라운드 번호를 앞으로 당겨 댕김
            cur.execute("UPDATE matches SET round = round - 1 WHERE round > ?", (round_to_del,))
            con.commit()
            recalculate_all()
            print(Fore.GREEN + f"[OK] 라운드 #{round_to_del} 기록이 완전히 삭제되었습니다.")
        else:
            print(Fore.YELLOW + "⚠ 삭제가 취소되었습니다.")
    except ValueError:
        print(Fore.RED + "⚠ 숫자를 입력해 주세요.")

def print_leaderboard():
    """터미널에 리더보드 출력"""
    print(Fore.CYAN + "\n=== REDLINER 스크림 리더보드 ===")
    cur.execute('''
        SELECT nickname, elo_combined, elo_1v1, wins_1v1, losses_1v1, elo_2v2, wins_2v2, losses_2v2
        FROM leaderboard ORDER BY elo_combined DESC
    ''')
    rows = cur.fetchall()
    if not rows:
        print("리더보드가 비어 있습니다.")
        return

    data = []
    for r in rows:
        nick, comb, elo1, w1, l1, elo2, w2, l2 = r
        wr1 = f"{(w1/(w1+l1)*100):.1f}%" if (w1+l1) > 0 else "0%"
        wr2 = f"{(w2/(w2+l2)*100):.1f}%" if (w2+l2) > 0 else "0%"
        data.append([nick, comb, f"{elo1} ({w1}W/{l1}L - {wr1})", f"{elo2} ({w2}W/{l2}L - {wr2})"])

    headers = ["Nickname", "Combined ELO", "1v1 Standings", "2v2 Standings"]
    print(tabulate(data, headers=headers, tablefmt="pretty"))

def reset_database():
    """데이터베이스 초기화"""
    confirm = input(Fore.RED + "⚠ 경고: 모든 리더보드, 매치 이력, ELO 계산 값이 영구 삭제됩니다. 계속하시겠습니까? (yes/no): ").strip().lower()
    if confirm == "yes":
        cur.execute("DELETE FROM matches")
        cur.execute("DELETE FROM leaderboard")
        cur.execute("DELETE FROM aftercalc")
        con.commit()
        export_to_csv()
        print(Fore.GREEN + "[OK] 데이터베이스 초기화가 완료되었습니다.")
    else:
        print(Fore.YELLOW + "⚠ 초기화가 취소되었습니다.")

def main():
    initialize_tables()
    # CSV 마이그레이션 체크
    cur.execute("SELECT COUNT(*) FROM matches")
    if cur.fetchone()[0] == 0:
        import_csv_to_db()
        
    while True:
        print(Fore.MAGENTA + "\n=== REDLINER ELO 매니저 메인 메뉴 ===")
        print("  1. 신규 스크림 경기 등록 (1대1 / 2대2)")
        print("  2. 특정 라운드 전적 기록 삭제")
        print("  3. 전체 ELO 값 재계산 (Recalculate All)")
        print("  4. 스크림 리더보드 조회 (터미널)")
        print("  5. 데이터베이스 완전 초기화")
        print("  6. 프로그램 종료")
        
        choice = input("\n>> 메뉴 선택: ").strip()
        if choice == '1':
            add_scrim_prompt()
        elif choice == '2':
            delete_round_prompt()
        elif choice == '3':
            recalculate_all()
        elif choice == '4':
            print_leaderboard()
        elif choice == '5':
            reset_database()
        elif choice == '6':
            print(Fore.GREEN + "프로그램을 종료합니다. 리더보드를 이용해주셔서 감사합니다!")
            break
        else:
            print(Fore.RED + "⚠ 올바르지 않은 메뉴 번호입니다.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n" + Fore.RED + "⌨️ 사용자에 의해 프로그램이 중단되었습니다.")
    finally:
        con.close()
