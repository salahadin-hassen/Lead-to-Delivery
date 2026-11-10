import type { Progress } from '../api/types';

export function ProgressBar({ progress }: { progress: Progress }) {
  return (
    <div className="progress">
      <div className="progress-track" role="progressbar" aria-valuemin={0} aria-valuemax={100}
           aria-valuenow={progress.percent} aria-label="Tasks completed">
        <div className="progress-fill" style={{ width: `${progress.percent}%` }} />
      </div>
      <span className="progress-text">{progress.completed_tasks}/{progress.total_tasks} tasks</span>
    </div>
  );
}
