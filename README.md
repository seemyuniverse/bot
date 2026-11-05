# 마스토돈 자동봇 파일

본 공개 레포지토리에서는 특정 커뮤니티/프로젝트의 고유 콘텐츠 및 세계관 데이터(보상 목록, 텍스트 등) 보호를 위해 `game_data.py` 파일이 제외되어 있습니다.  

프로젝트를 로컬이나 서버에서 직접 실행(`python app.py` 또는 `python bot.py`)하려면, 루트 디렉토리에 `game_data.py` 파일을 생성해야 `ModuleNotFoundError` 없이 정상 구동됩니다.  

### `game_data.py` 필수 정의 항목
- `DEFAULT_TEMPLATE`: 기본 답글 멘션 포맷 문자열
- `LEGACY_TEMPLATES`: 이전 버전 템플릿 세트(set)
- `GRADE_PROBABILITY`: 보상 등급별 가중치/확률 딕셔너리
- `LOOT`: 등급별 기본 보상 아이템 및 설명 목록
- `DEFAULT_OPTIONS`: 랜덤 요소(지역, 구역 유형, 부상 등) 목록
- `eul_reul(word)`: 한국어 받침 여부에 따른 조사('을/를') 자동 판별 함수
