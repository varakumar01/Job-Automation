import { useState } from 'react'
import { CaretDown, Trash } from '@phosphor-icons/react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { api } from '@/lib/api'

export function ResetMenu({ onReset }: { onReset: () => void }) {
  const [confirmHard, setConfirmHard] = useState<boolean | null>(null)

  async function doReset() {
    const hard = confirmHard ?? false
    setConfirmHard(null)
    await api.reset(hard)
    toast.success(hard ? 'Cleared everything' : 'Job data cleared')
    onReset()
  }

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger render={<Button variant="outline" size="lg" />}>
          <Trash className="size-4" />
          Reset
          <CaretDown className="size-3" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem onClick={() => setConfirmHard(false)}>
            Clear job data
          </DropdownMenuItem>
          <DropdownMenuItem variant="destructive" onClick={() => setConfirmHard(true)}>
            Clear everything (+ résumés, apply artifacts)
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <AlertDialog open={confirmHard !== null} onOpenChange={(open) => !open && setConfirmHard(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{confirmHard ? 'Clear everything?' : 'Clear job data?'}</AlertDialogTitle>
            <AlertDialogDescription>
              {confirmHard
                ? 'This deletes every scraped/matched job, plus every tailored résumé and apply artifact. This cannot be undone.'
                : 'This deletes every scraped/matched job from the store. Tailored résumés and apply artifacts are kept. This cannot be undone.'}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={doReset}>
              {confirmHard ? 'Clear everything' : 'Clear job data'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
