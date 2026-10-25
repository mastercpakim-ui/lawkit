# lawkit

[Korean Law MCP](https://github.com/chrisryugj/korean-law-mcp)에 붙여 쓰는 보완 도구입니다.

20년 경력 회계사가 세무 검토 실무에서 부딪힌 한계를 직접 설계해 해결했습니다. lawkit을 붙이면 Claude가 가산세·서식 의무를 **법·시행령·시행규칙 각각에서** 키워드로 찾아내고, 국세청 예규 여러 건을 **본문까지 한 번에** 받아 비교합니다.

Korean Law MCP도 4.15.10(2026-10-24 배포)부터 국세청 예규 본문을 한 건씩 받을 수 있습니다. 아래 표는 그 버전과 비교해 lawkit이 더하는 기능입니다.

| Korean Law MCP만 쓰면 | lawkit을 붙이면 |
|---|---|
| **조문 번호를 모르는 의무는 "없음"으로 끝나기 쉽습니다.** 원문은 조문 번호로 꺼내거나 법령 전문을 통째로 받아야 합니다. `search_ai_law`는 전체 법령에서 질문과 비슷한 조문을 찾아 주지만, 특정 법령·시행일 판으로 범위를 좁히는 옵션은 없습니다. 명세서 제출 의무·가산세처럼 시행령·시행규칙에 흩어진 규정은 아는 조문만 보고 "해당 없음"으로 결론 나기 쉽습니다 | **법·시행령·시행규칙을 각각 처음부터 끝까지 키워드로** 훑습니다(`law_grep`). 시행일 판을 골라 검색하고, 번호를 몰랐던 조문까지 찾아냅니다 |
| **예규 여러 건을 비교하려면 한 건씩 따로 열어야 합니다.** 제목이 같아도 사실관계(대여자가 개인인지 법인인지, 특수관계가 있는지 등)가 우리 사안과 다르면 결론이 정반대가 될 수 있어, 후보마다 본문을 확인해야 합니다 | **예규 20건까지를 한 번에** 받아 건별 사실관계·질의요지·회신 앞부분을 요약표 하나로 정리합니다(`nts_pack`). 받은 본문은 보관해 다시 받지 않습니다 |

판례·조세심판례·법령해석례·별표 서식·행정규칙·인용 검증은 Korean Law MCP로 하세요. 부칙·시행일 기준 조문·연혁·위임 조문·예규 한 건 본문은 두 도구 모두에 있습니다. 파이썬 스크립트에서는 `import lawkit`으로 바로 부를 수 있습니다(표준 라이브러리만 사용).

## 설치 (Claude Code · Claude 데스크톱 앱 Code 탭)

준비물
- Korean Law MCP를 이미 쓰고 있으면 됩니다. 법제처 인증키(`LAW_OC`)는 그 설정에서 그대로 가져다 씁니다. 커넥터로만 연결해 설정 파일에 키가 없으면, 설치 중에 Claude가 키를 한 번 물어봅니다
- Python 3.10 이상 (Windows는 설치할 때 "Add python.exe to PATH" 체크. 없으면 설치 중에 Claude가 안내합니다)

Claude Code(또는 데스크톱 앱 Code 탭의 로컬 세션)에 아래 한 줄을 붙여넣으세요. 설치 위치와 등록은 Claude가 이 README의 「Claude가 할 일」대로 처리합니다.

```
https://github.com/mastercpakim-ui/lawkit 설치해줘
```

등록이 끝나면 앱(또는 Claude Code)을 완전히 종료했다가 다시 켜세요.

## 이렇게 물어보세요

```
부가가치세법과 시행령에서 '등록 전'이 들어간 조문을 다 찾아줘
가지급금 인정이자 국세청 예규 10건을 본문까지 받아 사실관계·회신을 비교해줘
법인세법 시행규칙 제43조 부칙에서 적용례를 찾아줘
```

## 도구

| 도구 | 하는 일 |
|---|---|
| `law_article` · `law_articles` | 조문 원문(JSON). `efYd`로 특정 시행일 판 |
| `law_grep` | 선택한 시행판 전문에서 조문 단위 키워드 검색(any/all) |
| `law_versions` | 시행일 연혁(시행 예정 판 포함) |
| `law_addenda` | 부칙(적용례·경과조치) 원문, 키워드·최근 N개 필터 |
| `law_refs` | 조문 안의 참조 조문·위임 문구("대통령령으로 정하는") 추출 |
| `nts_search` | 국세청 예규 제목·본문 검색, 단어별 교집합 |
| `nts_pack` | 예규 여러 건(20건까지)의 요약표 또는 본문 묶음 파일 |

CLI로도 같은 결과를 얻습니다.

```bash
python lawkit.py article 법인세법 제73조 --efyd 20260101
python lawkit.py grep 부가가치세법 등록 공제하지 --all
python lawkit.py addenda "법인세법 시행규칙" --require 제43조 --grep 적용례 경과조치 --last 6
python lawkit.py nts 가지급금 --scope both
python lawkit.py --selftest
```

## 알아둘 것

- 법령명은 법제처 표기와 같아야 합니다(`상속세 및 증여세법`, `법인세법 시행규칙`). 다르면 후보 목록을 돌려줍니다.
- 부칙 검색 결과가 비면 "부칙 없음"이 아니라 **"부칙 미확인"**입니다. 키워드를 바꾸거나 부칙 전문을 읽으세요.
- 예규 본문은 국세청 사이트에서 받습니다. 한 번에 20건까지만 받고, 받은 본문은 `~/.cache/lawkit/nts/`에 보관해 다시 받지 않습니다. 대량 호출은 하지 마세요.
- 인증키는 요청 URL에만 들어가고 응답·오류문·캐시에는 남지 않습니다.
- 조회 결과는 원문 확인을 돕는 자료입니다. 세무 판단과 신고 책임은 사용자에게 있습니다.

---

## Claude가 할 일 (설치 절차)

> **Claude에게**: 각 Step은 진단 → 부족할 때만 조치 → 검증 순서로 한다. 무엇을 바꾸는지 한 줄로 알리고 진행한다. 실패하면 에러 원문과 함께 보고한다.

**Step 1. 받기·확인**
- 이 저장소를 홈 폴더 아래 `lawkit`으로 받는다(`git clone`, git이 없으면 GitHub의 ZIP을 받아 푼다)
- `python --version`(Windows) 또는 `python3 --version`(macOS·Linux)이 3.10 이상인지 본다. 없으면 설치를 안내하고 멈춘다

**Step 2. 인증키 (korean-law 설정에서 가져온다)**
- 아래 순서로 korean-law 항목의 `env.LAW_OC`를 찾아 같은 값을 쓴다. 값은 화면에 출력하지 않는다
  1. `~/.claude.json`의 `mcpServers.korean-law`
  2. Claude 데스크톱 앱 설정 `claude_desktop_config.json`(Windows `%APPDATA%\Claude\`, macOS `~/Library/Application Support/Claude/`)의 `mcpServers` 중 korean-law 항목
  3. 프로젝트 `.mcp.json`
- 셋 다 없으면 사용자에게 `LAW_OC` 값을 묻는다. 키는 저장소 안 어떤 파일에도 쓰지 않는다

**Step 3. MCP 등록**
- `claude` 명령이 있으면:
  ```
  claude mcp add --scope user lawkit -e LAW_OC=<키> -e PYTHONUTF8=1 -- python <lawkit 폴더 절대경로>/lawkit.py --mcp
  ```
  (macOS·Linux는 `python3`)
- 없으면(데스크톱 앱만 쓰는 경우) `~/.claude.json`을 백업한 뒤 JSON으로 읽어 최상위 `mcpServers`에 아래 항목만 더하고 저장한다(문자열 이어 붙이기 금지)
  ```json
  "lawkit": {"type": "stdio", "command": "python", "args": ["<lawkit 폴더 절대경로>/lawkit.py", "--mcp"], "env": {"LAW_OC": "<키>", "PYTHONUTF8": "1"}}
  ```

**Step 4. 검증**
- 환경변수 `LAW_OC`를 넣고 `python <lawkit 폴더>/lawkit.py --selftest` → 마지막 줄 `selftest OK`. 네트워크가 막혀 있으면 오프라인 검사만 돌고 `SKIP`이 찍힌다(실패 아님)
- "앱(또는 Claude Code)을 완전히 종료했다가 다시 켜면 lawkit 도구가 잡힙니다"라고 안내하고 끝낸다

---

만든 사람: NuttyCPA Jihye · 라이선스: MIT
