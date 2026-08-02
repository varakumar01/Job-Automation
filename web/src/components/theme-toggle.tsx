import { useTheme } from 'next-themes'
import { Moon, Sun } from '@phosphor-icons/react'
import { Button } from '@/components/ui/button'

export function ThemeToggle() {
  const { resolvedTheme, setTheme } = useTheme()
  return (
    <Button
      variant="ghost"
      size="icon-lg"
      aria-label="Toggle theme"
      onClick={() => setTheme(resolvedTheme === 'dark' ? 'light' : 'dark')}
    >
      <Sun className="size-5 dark:hidden" weight="bold" />
      <Moon className="hidden size-5 dark:block" weight="bold" />
    </Button>
  )
}
