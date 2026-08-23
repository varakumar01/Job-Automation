# Bash tab-completion for the job-search pipeline `main.py`.
#
# Install — add ONE of these to ~/.bashrc:
#     source /home/tony/github/job-search/completions/main.py.bash
# Then (main.py is executable, has a shebang):
#     ./main.py <TAB>            → commands (bare + --tag forms)
#     ./main.py apply --<TAB>    → that command's flags
#     ./main.py prep --llm <TAB> → auto claude nvidia grok deepseek api
#
# It registers on `main.py`, `./main.py`, and (optionally) an alias `js`. For the
# `.venv/bin/python3 main.py …` form, add an alias to ~/.bashrc so completion works:
#     alias js='/home/tony/github/job-search/.venv/bin/python3 /home/tony/github/job-search/main.py'
#     complete -F _jobsearch_main_py js
#
# Keep the command/flag lists below in sync with main.py's argparse (the `COMMANDS`
# tuple + each subparser's flags).

_jobsearch_main_py() {
    local cur prev
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"

    local commands="search lists prep apply log applied report reject rejected keys sources stats rank"

    # Per-command flags (mirror main.py's subparsers).
    local f_search="--locations --queries --days --limit --source --workers --recheck --llm --recheck-providers"
    local f_lists="--raw"
    local f_prep="--llm --recheck-providers --eligible --llm-best --needs-mod --stretch --jobs --modify-resume --limit"
    local f_apply="--limit --source --query --jobs"
    local f_log="--job --outcome --note --screenshot"
    local f_applied=""
    local f_report=""
    local f_reject="--by-llm"
    local f_rejected=""
    local f_keys="--llm --reset"
    local f_sources=""
    local f_stats=""
    local f_rank="--llm --recheck-providers --limit --eligible --jobs --save"

    # Identify the command token (first word matching a command, accepting the --tag form).
    local cmd="" i w
    for (( i=1; i < COMP_CWORD; i++ )); do
        w="${COMP_WORDS[i]}"
        w="${w#--}"                       # accept `--apply` == `apply`
        if [[ " $commands " == *" $w "* ]]; then cmd="$w"; break; fi
    done

    # Still choosing the command → offer bare + --tag forms.
    if [[ -z "$cmd" ]]; then
        local dashed="" c
        for c in $commands; do dashed+="--$c "; done
        COMPREPLY=( $(compgen -W "$commands $dashed" -- "$cur") )
        return 0
    fi

    # Value completions for known enum flags. --llm differs by command: prep
    # additionally allows `claude` (session mode, manual); rank/search do not.
    case "$prev" in
        --llm)
            if [[ "$cmd" == "prep" ]]; then
                COMPREPLY=( $(compgen -W "auto claude nvidia grok deepseek api" -- "$cur") )
            else
                COMPREPLY=( $(compgen -W "auto nvidia grok deepseek api" -- "$cur") )
            fi
            return 0 ;;
        --outcome) COMPREPLY=( $(compgen -W "applied skipped failed" -- "$cur") ); return 0 ;;
        --source)  COMPREPLY=( $(compgen -W "linkedin wellfound" -- "$cur") ); return 0 ;;
    esac

    # Otherwise complete this command's flags.
    local var="f_${cmd}"
    COMPREPLY=( $(compgen -W "${!var}" -- "$cur") )
    return 0
}

complete -F _jobsearch_main_py main.py
complete -F _jobsearch_main_py ./main.py
